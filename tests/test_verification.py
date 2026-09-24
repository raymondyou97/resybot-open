import io
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from client.booking_state import reservation_reference
from client.config_store import load_data, save_data
from client.control import RunControl
from client.reservations import cancel_verified, resolve_claim
from client.task_executor import execute_task
from client.verification import VerificationError, matching_reservation, slot_datetime, upcoming
from support import OfflineTest, availability, quote, reservation, task


def response(data):
    return Mock(status_code=200, json=Mock(return_value=data))


class VerificationTests(OfflineTest):
    def test_empty_and_complete_account_responses_are_supported(self):
        for rows in ([], [reservation()], [reservation(time_slot='18:30', venue={'id': {'resy': 123}})]):
            data = {'reservations': rows}
            with patch('client.verification.requests.get', return_value=response(data)):
                self.assertEqual(upcoming({}), data)

    def test_malformed_rows_fail_closed_without_echoing_values(self):
        malformed = [
            None,
            [],
            {},
            reservation(venue=None),
            reservation(venue={'id': True}),
            reservation(day='2099-02-30'),
            reservation(day='fixture-private-value'),
            reservation(time_slot='18:30:01'),
            reservation(time_slot='18:30-private-value'),
            reservation(time_slot='24:00:00'),
            reservation(time_slot=None),
            reservation(num_seats=True),
            reservation(num_seats='2'),
            reservation(num_seats=0),
            reservation(reservation_id=None),
            reservation(reservation_id=''),
            reservation(reservation_id='   '),
            reservation(reservation_id={'private': 'fixture-private-value'}),
            reservation(reservation_id=True),
        ]
        for index, row in enumerate(malformed):
            with (
                self.subTest(case=index),
                patch('client.verification.requests.get', return_value=response({'reservations': [row]})),
            ):
                with self.assertRaises(VerificationError) as error:
                    upcoming({})
                self.assertNotIn('fixture-private-value', str(error.exception))

    def test_matching_validates_every_row_not_only_the_matching_row(self):
        with self.assertRaises(VerificationError):
            matching_reservation({'reservations': [reservation(), {}]}, '123', '2099-01-01', '18:30', 2)
        self.assertEqual(
            matching_reservation({'reservations': [reservation()]}, '123', '2099-01-01', '18:30', 2),
            reservation_reference(reservation()),
        )

    def test_slot_precision_is_not_silently_truncated(self):
        for clock in ('18:30:01', '18:30:00.001'):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                slot_datetime({'date': {'start': f'2099-01-01 {clock}'}}, '2099-01-01')
        self.assertEqual(slot_datetime({'date': {'start': '2099-01-01 18:30:00'}}, '2099-01-01'), '18:30')

    def test_reference_requires_nonempty_scalar_identifier(self):
        for value in (None, '', ' ', {}, ['fixture'], True, False, 0, -1):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                reservation_reference(reservation(reservation_id=value))
        self.assertTrue(reservation_reference(reservation(reservation_id=None, id=123)))

    def test_identity_does_not_depend_on_rotating_cancellation_token(self):
        first = reservation(resy_token='fixture-first-token')
        refreshed = reservation(resy_token='fixture-refreshed-token')
        self.assertEqual(reservation_reference(first), reservation_reference(refreshed))
        with self.assertRaises(ValueError):
            reservation_reference(reservation(reservation_id=None))

    def test_malformed_preflight_never_enters_checkout(self):
        with (
            patch(
                'client.verification.requests.get',
                side_effect=[response(availability()), response({'reservations': [{}]})],
            ),
            patch('client.task_executor.get_details') as details,
            patch('client.task_executor.book_reservation') as book,
            redirect_stdout(io.StringIO()),
        ):
            result = execute_task(task(), control=RunControl(120), dry_run=False, state=self.state)
        self.assertEqual(result, 'failed')
        details.assert_not_called()
        book.assert_not_called()
        self.assertEqual(self.state.rows(), [])

    def test_malformed_confirmation_keeps_submission_hold(self):
        with (
            patch(
                'client.verification.requests.get',
                side_effect=[
                    response(availability()),
                    response({'reservations': []}),
                    response({'reservations': [{}]}),
                ],
            ),
            patch('client.task_executor.get_details', return_value=quote()),
            patch('client.task_executor.book_reservation', return_value={'submitted': True}),
            redirect_stdout(io.StringIO()),
        ):
            result = execute_task(task(), control=RunControl(120), dry_run=False, state=self.state)
        self.assertEqual(result, 'awaiting-verification')
        self.assertEqual(self.state.rows()[0]['status'], 'submitted')

    def test_malformed_post_cancellation_response_keeps_cache_and_hold(self):
        ref = reservation_reference(reservation())
        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.submitted(key)
        self.state.confirmed(key, ref)
        record = {
            'reference': ref,
            'day': '2099-01-01',
            'time_slot': '18:30',
            'venue_id': '123',
            'num_seats': 2,
        }
        save_data('resrevations.json', [record])
        with (
            patch(
                'client.verification.requests.get',
                side_effect=[
                    response({'reservations': [reservation()]}),
                    response({'reservations': [reservation(reservation_id={})]}),
                ],
            ),
            patch('client.reservations.requests.post', return_value=Mock(status_code=200)),
            self.assertRaises(VerificationError),
        ):
            cancel_verified(task(), record, state=self.state)
        self.assertTrue(load_data('resrevations.json', [])[0]['cancellation_pending'])
        self.assertEqual(self.state.get(key)['status'], 'cancellation-pending')

    def test_malformed_account_cannot_release_an_aged_hold(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.submitted(key)
        with self.state.connect() as db:
            db.execute('UPDATE claims SET created=0 WHERE id=?', (key,))
        with patch('client.verification.requests.get', return_value=response({'reservations': [{}]})):
            with self.assertRaises(VerificationError):
                resolve_claim(self.state, key, task(), checked_no_booking=True)
        self.assertEqual(self.state.get(key)['status'], 'submitted')
