"""Slot lookup tests with no network, account files, or real booking calls."""

from datetime import date, timedelta
import random
import types
import unittest
from unittest.mock import Mock

from test_optional_proxies import load_functions


class EndCycle(BaseException):
    pass


def response(payload, status=200):
    return types.SimpleNamespace(status_code=status, json=lambda: payload)


class AvailabilityTests(unittest.TestCase):
    def worker(self, responses):
        return load_functions(
            'client/task_executor.py',
            {'execute_task', 'format_proxy', 'reservation_dates'},
            date=date, timedelta=timedelta, random=random,
            requests=types.SimpleNamespace(get=Mock(side_effect=responses)),
            time=types.SimpleNamespace(sleep=Mock(side_effect=EndCycle)),
            get_details=Mock(return_value='offline-book-token'),
            book_reservation=Mock(return_value={'reservation_id': 'offline-fixture'}),
            send_discord_notification=Mock(), print=Mock(),
        )

    def task(self, **overrides):
        return {
            'auth_token': 'offline-fixture', 'payment_id': 0,
            'restaurant_id': '123', 'party_sz': 2,
            'start_date': '2030-01-01', 'end_date': '2030-01-01',
            'start_time': 18, 'end_time': 19, 'delay': 2000,
            **overrides,
        }

    def run_worker(self, worker, **overrides):
        worker.execute_task(self.task(**overrides), '', '', [], '')

    def test_uses_slot_endpoint_without_calendar_gate(self):
        worker = self.worker([response({'results': {'venues': []}})])
        with self.assertRaises(EndCycle):
            self.run_worker(worker)
        worker.requests.get.assert_called_once()
        call = worker.requests.get.call_args
        self.assertEqual(call.args, ('https://api.resy.com/4/find',))
        self.assertEqual(call.kwargs['params'], {
            'lat': 0, 'long': 0, 'day': '2030-01-01', 'party_size': 2, 'venue_id': '123',
        })
        self.assertEqual(call.kwargs['timeout'], (5, 15))
        worker.get_details.assert_not_called()
        worker.book_reservation.assert_not_called()
        worker.time.sleep.assert_called_once_with(2.0)
        worker.print.assert_called_once_with(
            '[Restaurant 123] No matching slot for 2030-01-01; waiting 2 seconds before the next check.'
        )

    def test_no_slot_log_uses_restaurant_name_and_id(self):
        worker = self.worker([response({'results': {'venues': [
            {'venue': {'name': 'Dear Margo'}, 'slots': []},
        ]}})])
        with self.assertRaises(EndCycle):
            self.run_worker(worker)
        worker.print.assert_called_once_with(
            '[Dear Margo (ID 123)] No matching slot for 2030-01-01; waiting 2 seconds before the next check.'
        )

    def test_name_is_retained_when_next_date_has_no_venue(self):
        worker = self.worker([
            response({'results': {'venues': [
                {'venue': {'name': 'Dear Margo'}, 'slots': []},
            ]}}),
            response({'results': {'venues': []}}),
        ])
        worker.time.sleep.side_effect = [None, EndCycle()]
        with self.assertRaises(EndCycle):
            self.run_worker(worker, end_date='2030-01-02')
        worker.print.assert_any_call(
            '[Dear Margo (ID 123)] No matching slot for 2030-01-02; waiting 2 seconds before the next check.'
        )

    def test_checks_each_requested_day_with_delay_between_requests(self):
        empty = response({'results': {'venues': [{'slots': []}]}})
        worker = self.worker([empty, empty])
        worker.time.sleep.side_effect = [None, EndCycle()]
        with self.assertRaises(EndCycle):
            self.run_worker(worker, end_date='2030-01-02')
        self.assertEqual(
            [call.kwargs['params']['day'] for call in worker.requests.get.call_args_list],
            ['2030-01-01', '2030-01-02'],
        )
        self.assertEqual(worker.time.sleep.call_count, 2)
        worker.book_reservation.assert_not_called()

    def test_repeats_date_range_only_after_waiting(self):
        empty = response({'results': {'venues': []}})
        worker = self.worker([empty, empty])
        worker.time.sleep.side_effect = [None, EndCycle()]
        with self.assertRaises(EndCycle):
            self.run_worker(worker)
        self.assertEqual(worker.requests.get.call_count, 2)
        self.assertEqual(worker.time.sleep.call_count, 2)

    def test_zero_delay_still_waits_one_second(self):
        worker = self.worker([response({'results': {'venues': []}})])
        with self.assertRaises(EndCycle):
            self.run_worker(worker, delay=0)
        worker.time.sleep.assert_called_once_with(1.0)

    def test_http_errors_stop_without_retry_or_booking(self):
        for status in [401, 403, 429, 500]:
            with self.subTest(status=status):
                worker = self.worker([response({}, status=status)])
                self.run_worker(worker)
                worker.requests.get.assert_called_once()
                worker.time.sleep.assert_not_called()
                worker.get_details.assert_not_called()
                worker.book_reservation.assert_not_called()
                summary = worker.send_discord_notification.call_args.kwargs['summary']
                self.assertIn(str(status), summary)

    def test_malformed_response_stops_without_booking(self):
        for payload in [{}, [], {'results': {}}, {'results': {'venues': None}}]:
            with self.subTest(payload=payload):
                worker = self.worker([response(payload)])
                self.run_worker(worker)
                worker.requests.get.assert_called_once()
                worker.book_reservation.assert_not_called()
                worker.time.sleep.assert_not_called()
                worker.send_discord_notification.assert_called_once()

    def test_matching_slot_passes_correct_day_to_mocked_checkout_once(self):
        token = '/'.join(['offline'] * 8 + ['18:30'])
        worker = self.worker([response({'results': {'venues': [
            {'venue': {'name': 'Dear Margo'}, 'slots': [
                {'config': {'token': token}}, {'config': {'token': token}},
            ]},
        ]}})])
        self.run_worker(worker)
        worker.get_details.assert_called_once()
        self.assertEqual(worker.get_details.call_args.args[:4], ('2030-01-01', 2, token, '123'))
        worker.book_reservation.assert_called_once()
        self.assertEqual(worker.book_reservation.call_args.args[3], '2030-01-01')
        worker.requests.get.assert_called_once()
        worker.time.sleep.assert_not_called()
        summary = worker.send_discord_notification.call_args.kwargs['summary']
        self.assertIn('[Dear Margo (ID 123)]', summary)
        self.assertIn('stopping this worker', summary)

    def test_outside_time_window_does_not_enter_checkout(self):
        token = '/'.join(['offline'] * 8 + ['12:00'])
        worker = self.worker([response({'results': {'venues': [
            {'slots': [{'config': {'token': token}}]},
        ]}})])
        with self.assertRaises(EndCycle):
            self.run_worker(worker)
        worker.get_details.assert_not_called()
        worker.book_reservation.assert_not_called()

    def test_date_range_validation(self):
        worker = self.worker([])
        self.assertEqual(worker.reservation_dates('2028-02-28', '2028-03-01'),
                         ['2028-02-28', '2028-02-29', '2028-03-01'])
        for start, end in [('2030-01-02', '2030-01-01'),
                           ('2030-01-01', '2030-02-01'),
                           ('not-a-date', '2030-01-01')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.run_worker(worker, start_date=start, end_date=end)
        worker.requests.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
