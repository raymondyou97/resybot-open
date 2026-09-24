import io
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from client.control import RunControl
from client import task_executor as worker
from support import OfflineTest, availability, quote, reservation, task


class AvailabilityTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.control = RunControl(120)
        self.control.wait = Mock(return_value=False)
        self.get = self.enter(
            patch.object(
                worker.requests,
                'get',
                return_value=Mock(status_code=200, json=Mock(return_value=availability())),
            )
        )
        self.details = self.enter(patch.object(worker, 'get_details', return_value=quote()))
        self.book = self.enter(patch.object(worker, 'book_reservation', return_value={'submitted': True}))
        self.account = self.enter(
            patch.object(
                worker,
                'upcoming',
                side_effect=[
                    {'reservations': []},
                    {'reservations': [reservation()]},
                ],
            )
        )
        self.enter(patch.object(worker, 'send_discord_notification'))

    def enter(self, context):
        value = context.start()
        self.addCleanup(context.stop)
        return value

    def run_task(self, data=None, dry_run=False):
        with redirect_stdout(io.StringIO()):
            return worker.execute_task(
                data or task(), control=self.control, dry_run=dry_run, state=self.state
            )

    def test_verified_success_stops_and_persists(self):
        self.assertEqual(self.run_task(), 'confirmed')
        self.book.assert_called_once()
        self.get.assert_called_once()
        self.assertEqual(self.state.rows()[0]['status'], 'confirmed')
        self.assertEqual(self.run_task(), 'blocked')
        self.book.assert_called_once()

    def test_availability_identifies_the_client_explicitly(self):
        self.assertEqual(self.run_task(), 'confirmed')
        self.assertEqual(
            self.get.call_args.kwargs['headers']['User-Agent'],
            'resybot-open/1.0 (personal reservation client)',
        )

    def test_default_is_non_mutating_dry_run(self):
        with redirect_stdout(io.StringIO()):
            result = worker.execute_task(task(), control=self.control)
        self.assertEqual(result, 'dry-run-complete')
        self.details.assert_not_called()
        self.book.assert_not_called()
        self.account.assert_not_called()
        self.assertEqual(self.state.rows(), [])

    def test_dry_run_ignores_unconfigured_fee_policy(self):
        self.assertEqual(self.run_task(task(accept_terms=False), dry_run=True), 'dry-run-complete')
        self.book.assert_not_called()

    def test_invalid_policy_blocks_before_network(self):
        self.assertEqual(self.run_task(task(accept_terms=False)), 'failed')
        self.get.assert_not_called()

    def test_price_failure_releases_only_unsubmitted_claim(self):
        self.details.return_value = quote(total=500)
        self.assertEqual(self.run_task(), 'failed')
        self.book.assert_not_called()
        self.assertEqual(self.state.rows()[0]['status'], 'released')

    def test_missing_quote_fields_stop_without_submission(self):
        self.details.return_value = {'response_value': 'fixture', 'details': {}}
        self.assertEqual(self.run_task(), 'failed')
        self.book.assert_not_called()

    def test_booking_id_without_account_confirmation_keeps_hold(self):
        self.account.side_effect = [{'reservations': []}, {'reservations': []}]
        self.assertEqual(self.run_task(), 'awaiting-verification')
        self.assertEqual(self.state.rows()[0]['status'], 'submitted')
        self.assertEqual(self.run_task(), 'blocked')
        self.book.assert_called_once()

    def test_timeout_after_submission_keeps_hold(self):
        self.book.side_effect = TimeoutError('sensitive error must not be printed')
        self.assertEqual(self.run_task(), 'awaiting-verification')
        self.assertEqual(self.state.rows()[0]['status'], 'submitted')

    def test_wrong_party_does_not_confirm(self):
        self.account.side_effect = [{'reservations': []}, {'reservations': [reservation(num_seats=3)]}]
        self.assertEqual(self.run_task(), 'awaiting-verification')

    def test_existing_upcoming_reservation_prevents_duplicate(self):
        self.account.side_effect = [{'reservations': [reservation()]}]
        self.assertEqual(self.run_task(), 'blocked')
        self.book.assert_not_called()

    def test_stop_before_worker_does_not_query(self):
        self.control.stop()
        self.assertEqual(self.run_task(), 'stopped')
        self.get.assert_not_called()

    def test_stop_during_availability_does_not_enter_checkout(self):
        def get(*args, **kwargs):
            self.control.stop()
            return Mock(status_code=200, json=lambda: availability())

        self.get.side_effect = get
        self.assertEqual(self.run_task(), 'stopped')
        self.details.assert_not_called()

    def test_stop_during_quote_does_not_submit(self):
        def details(*args, **kwargs):
            self.control.stop()
            return quote()

        self.details.side_effect = details
        self.assertEqual(self.run_task(), 'stopped')
        self.book.assert_not_called()
        self.assertEqual(self.state.rows()[0]['status'], 'released')

    def test_expiry_prevents_queries(self):
        self.control.deadline = 0
        self.assertEqual(self.run_task(), 'stopped')
        self.get.assert_not_called()

    def test_http_errors_stop_without_retry(self):
        for status in [401, 403, 429, 500]:
            with self.subTest(status=status):
                self.get.reset_mock()
                self.get.return_value.status_code = status
                self.assertEqual(self.run_task(), 'failed')
                self.get.assert_called_once()
                self.book.assert_not_called()

    def test_wrong_venue_and_slot_date_never_book(self):
        self.get.return_value.json.return_value = availability(venue_id=999)
        self.control.wait.return_value = True
        self.assertEqual(self.run_task(), 'stopped')
        self.book.assert_not_called()
        self.get.return_value.json.return_value = availability(
            slots=[
                {'date': {'start': '2099-01-02 18:30:00'}, 'config': {'token': 'fixture'}},
            ]
        )
        self.assertEqual(self.run_task(), 'failed')
        self.book.assert_not_called()

    def test_past_dates_are_not_queried(self):
        self.assertEqual(self.run_task(task(start_date='2000-01-01', end_date='2000-01-02')), 'stopped')
        self.get.assert_not_called()

    def test_expired_same_day_slot_never_enters_checkout(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        self.get.return_value.json.return_value = availability(
            slots=[
                {'date': {'start': f'{today} 00:00:00'}, 'config': {'token': 'fixture'}},
            ]
        )
        self.assertEqual(
            self.run_task(task(start_date=today, end_date=today, start_time=0), dry_run=True),
            'dry-run-complete',
        )
        self.details.assert_not_called()

    def test_aware_slot_time_is_converted_to_venue_timezone(self):
        from client.verification import slot_datetime

        self.assertEqual(
            slot_datetime({'date': {'start': '2099-01-02T00:30:00+00:00'}}, '2099-01-01'), '19:30'
        )
        with self.assertRaises(ValueError):
            slot_datetime({'date': {'start': '2099-01-02T00:30:00+00:00'}}, '2099-01-02')

    def test_exact_minute_window_includes_2000_but_not_2001(self):
        for clock, expected in [
            ('16:59', False),
            ('17:00', True),
            ('19:45', True),
            ('20:00', True),
            ('20:01', False),
            ('20:59', False),
        ]:
            self.get.return_value.json.return_value = availability(
                slots=[
                    {'date': {'start': f'2099-01-01 {clock}:00'}, 'config': {'token': 'fixture'}},
                ]
            )
            output = io.StringIO()
            with self.subTest(clock=clock), redirect_stdout(output):
                result = worker.execute_task(
                    task(start_time='17:00', end_time='20:00'), control=self.control, dry_run=True
                )
            self.assertEqual(result, 'dry-run-complete')
            self.assertEqual('DRY RUN: matching slot' in output.getvalue(), expected)
        self.book.assert_not_called()

    def test_logs_identify_restaurant_without_credentials(self):
        self.get.return_value.json.return_value = availability(slots=[])
        output = io.StringIO()
        with redirect_stdout(output):
            worker.execute_task(task(), control=self.control, dry_run=True)
        self.assertIn('Dear Margo (ID 123)', output.getvalue())
        self.assertNotIn(task()['auth_token'], output.getvalue())
        self.assertNotIn('fixture-slot-token', output.getvalue())

    def test_dates_and_polling_remain_bounded(self):
        self.assertEqual(
            worker.reservation_dates('2028-02-28', '2028-03-01'), ['2028-02-28', '2028-02-29', '2028-03-01']
        )
        for start, end in [('2030-01-02', '2030-01-01'), ('2030-01-01', '2030-02-01')]:
            with self.assertRaises(ValueError):
                worker.reservation_dates(start, end)
