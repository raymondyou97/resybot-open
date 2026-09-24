import io
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import Mock, patch

from client.availability import calendar_available_dates
from client.control import RunControl
from client import task_executor as worker
from support import OfflineTest, availability, quote, reservation, task


def calendar(days=('2099-01-01',), available=(), horizon='2099-01-01'):
    return {
        'last_calendar_day': horizon,
        'scheduled': [
            {'date': day, 'inventory': {'reservation': 'available' if day in available else 'sold-out'}}
            for day in days
        ],
    }


def response(data=None, status=200):
    return Mock(status_code=status, json=Mock(return_value=data))


class CalendarTests(OfflineTest):
    def test_filters_entire_range_and_skips_unreleased_dates(self):
        days = worker.reservation_dates('2099-09-26', '2099-10-06')
        data = calendar(days[:5], available=[days[1]], horizon='2099-09-30')
        data['scheduled'][2]['inventory']['reservation'] = 'closed'
        self.assertEqual(calendar_available_dates(data, days), [days[1]])
        self.assertEqual(calendar_available_dates(calendar([], horizon='2099-09-25'), days), [])

    def test_unknown_partial_duplicate_and_out_of_range_data_fail_closed(self):
        partial = calendar([])
        unknown = calendar()
        unknown['scheduled'][0]['inventory']['reservation'] = 'unknown'
        duplicate = calendar()
        duplicate['scheduled'].append(deepcopy(duplicate['scheduled'][0]))
        outside = calendar(['2099-01-02'], horizon='2099-01-02')
        missing_horizon = calendar()
        del missing_horizon['last_calendar_day']
        for index, data in enumerate([{}, None, partial, unknown, duplicate, outside, missing_horizon]):
            with self.subTest(case=index), self.assertRaises(ValueError):
                calendar_available_dates(data, ['2099-01-01'])

    def test_no_inventory_uses_one_request_for_whole_range_then_waits(self):
        days = worker.reservation_dates('2099-01-01', '2099-01-11')
        control = RunControl('forever')
        control.wait = Mock(return_value=True)
        with (
            patch.object(
                worker.requests, 'get', return_value=response(calendar(days, horizon=days[-1]))
            ) as get,
            patch.object(worker, 'get_details') as details,
            patch.object(worker, 'book_reservation') as book,
            redirect_stdout(io.StringIO()),
        ):
            result = worker.execute_task(
                task(availability_mode='calendar', end_date=days[-1], delay=60000),
                control=control,
                dry_run=False,
                state=self.state,
            )
        self.assertEqual(result, 'stopped')
        get.assert_called_once()
        self.assertTrue(get.call_args.args[0].endswith('/4/venue/calendar'))
        self.assertEqual(get.call_args.kwargs['params']['end_date'], days[-1])
        control.wait.assert_called_once_with(60)
        details.assert_not_called()
        book.assert_not_called()

    def test_only_available_dates_get_exact_slot_lookups(self):
        days = ['2099-01-01', '2099-01-02']
        control = RunControl(120)
        control.wait = Mock(return_value=False)
        with (
            patch.object(
                worker.requests,
                'get',
                side_effect=[response(calendar(days, [days[1]], days[-1])), response(availability(slots=[]))],
            ) as get,
            patch.object(worker, 'book_reservation') as book,
            redirect_stdout(io.StringIO()),
        ):
            result = worker.execute_task(
                task(availability_mode='calendar', end_date=days[-1]), control=control, dry_run=True
            )
        self.assertEqual(result, 'dry-run-complete')
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs['params']['day'], days[1])
        book.assert_not_called()

    def test_calendar_never_replaces_quote_and_account_confirmation(self):
        with (
            patch.object(
                worker.requests,
                'get',
                side_effect=[response(calendar(available=['2099-01-01'])), response(availability())],
            ),
            patch.object(
                worker, 'upcoming', side_effect=[{'reservations': []}, {'reservations': [reservation()]}]
            ),
            patch.object(worker, 'get_details', return_value=quote()) as details,
            patch.object(worker, 'book_reservation', return_value={'submitted': True}) as book,
            redirect_stdout(io.StringIO()),
        ):
            result = worker.execute_task(task(availability_mode='calendar'), dry_run=False, state=self.state)
        self.assertEqual(result, 'confirmed')
        details.assert_called_once()
        book.assert_called_once()
        self.assertEqual(self.state.rows()[0]['status'], 'confirmed')

    def test_access_errors_do_not_fall_back_or_retry(self):
        for code in (401, 403, 429):
            with (
                self.subTest(code=code),
                patch.object(worker.requests, 'get', return_value=response(status=code)) as get,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    worker.execute_task(task(availability_mode='calendar'), dry_run=True), 'failed'
                )
            get.assert_called_once()

    def test_unavailable_calendar_falls_back_once_to_paced_date_checks(self):
        control = RunControl(120)
        control.wait = Mock(side_effect=[False, True])
        with (
            patch.object(
                worker.requests,
                'get',
                side_effect=[
                    response(status=500),
                    response(availability(slots=[])),
                    response(availability(slots=[])),
                ],
            ) as get,
            redirect_stdout(io.StringIO()),
        ):
            result = worker.execute_task(
                task(availability_mode='calendar', delay=60000),
                control=control,
                dry_run=False,
                state=self.state,
            )
        self.assertEqual(result, 'stopped')
        urls = [call.args[0] for call in get.call_args_list]
        self.assertEqual(sum(url.endswith('/4/venue/calendar') for url in urls), 1)
        self.assertEqual(sum(url.endswith('/4/find') for url in urls), 2)
        self.assertEqual([call.args for call in control.wait.call_args_list], [(60,), (60,)])

    def test_stop_during_calendar_never_queries_slots(self):
        control = RunControl(120)

        def get(*args, **kwargs):
            control.stop()
            return response(calendar(available=['2099-01-01']))

        with patch.object(worker.requests, 'get', side_effect=get) as network, redirect_stdout(io.StringIO()):
            self.assertEqual(
                worker.execute_task(task(availability_mode='calendar'), control=control, dry_run=True),
                'stopped',
            )
        network.assert_called_once()
