from datetime import datetime
import threading
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from client.control import RunControl, parse_duration
from client.config_store import load_data, save_data
from client.scheduling import TaskManager, next_once, task_id, trigger_for
from support import OfflineTest, task


class SchedulingTests(OfflineTest):
    def test_control_deadline_uses_monotonic_time(self):
        clock = Mock(return_value=10)
        control = RunControl(5, clock=clock)
        self.assertFalse(control.stopped())
        clock.return_value = 15
        self.assertTrue(control.stopped())

    def test_forever_has_no_deadline_but_keeps_http_timeouts_and_manual_stop(self):
        clock = Mock(return_value=10)
        control = RunControl('forever', clock=clock)
        clock.return_value = 10**12
        self.assertFalse(control.stopped())
        self.assertEqual(control.timeout(), (3, 5))
        done = threading.Event()
        thread = threading.Thread(target=lambda: (control.wait(60), done.set()))
        thread.start()
        control.stop()
        self.assertTrue(done.wait(1))
        thread.join(1)
        self.assertTrue(control.stopped())

    def test_only_explicit_forever_removes_deadline(self):
        self.assertEqual(parse_duration('forever'), 'forever')
        self.assertEqual(parse_duration('120'), 120)
        for duration in (None, True, float('inf'), float('nan'), 0, -1, 'unlimited'):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                RunControl(duration)

    def test_forever_schedule_duration_survives_private_storage(self):
        config = task()
        save_data('tasks.json', [config])
        spec = {
            'id': 'fixture',
            'task_id': task_id(config),
            'repeat': 'Daily',
            'time': '00:00',
            'duration': 'forever',
            'dry_run': True,
        }
        manager = TaskManager(Mock())
        with patch('client.scheduling.BackgroundScheduler'):
            manager.add(spec)
        saved = load_data('schedules.json', [])[0]
        manager.start = Mock()
        manager.dispatch(saved)
        self.assertEqual(manager.start.call_args.kwargs['duration'], 'forever')

    def test_stop_interrupts_sleep(self):
        control = RunControl(60)
        done = threading.Event()
        thread = threading.Thread(target=lambda: (control.wait(60), done.set()))
        thread.start()
        control.stop()
        self.assertTrue(done.wait(1))
        thread.join(1)

    def test_manager_only_reports_stopped_after_thread_finishes(self):
        entered, release = threading.Event(), threading.Event()

        def worker(*args, control=None, **kwargs):
            entered.set()
            release.wait(2)

        manager = TaskManager(worker)
        self.addCleanup(manager.close)
        self.assertTrue(manager.start('one', [task()], [], {}))
        self.assertTrue(entered.wait(1))
        self.assertFalse(manager.start('one', [task()], [], {}))
        self.assertFalse(manager.stop('one', timeout=0))
        self.assertEqual(manager.statuses()['one'], 'stopping')
        release.set()
        self.assertTrue(manager.stop('one', timeout=1))

    def test_duration_is_enforced_during_worker_not_after_it(self):
        done = threading.Event()

        def worker(*args, control=None, **kwargs):
            control.wait(100)
            self.assertTrue(control.stopped())
            done.set()

        manager = TaskManager(worker)
        self.addCleanup(manager.close)
        manager.start('one', [task()], [], {}, duration=0.05)
        self.assertTrue(done.wait(1))

    def test_weekly_trigger_only_runs_selected_weekday(self):
        zone = ZoneInfo('America/New_York')
        trigger, _ = trigger_for({'repeat': 'Weekly', 'time': '18:00', 'weekday': 'fri'})
        first = trigger.get_next_fire_time(None, datetime(2030, 1, 1, tzinfo=zone))
        second = trigger.get_next_fire_time(first, first)
        self.assertEqual(first.weekday(), 4)
        self.assertEqual((second.date() - first.date()).days, 7)

    def test_once_uses_next_day_when_time_has_passed(self):
        now = datetime(2030, 1, 1, 20, tzinfo=ZoneInfo('America/New_York'))
        self.assertTrue(next_once('19:00', now).startswith('2030-01-02T19:00'))

    def test_schedule_stable_identity_is_not_list_index(self):
        first, second = task(restaurant_id='123'), task(restaurant_id='456')
        save_data('tasks.json', [second, first])
        manager = TaskManager(Mock())
        manager.start = Mock()
        spec = {'id': 'fixture', 'task_id': task_id(first), 'repeat': 'Daily', 'duration': 1, 'dry_run': True}
        manager.dispatch(spec)
        self.assertEqual(manager.start.call_args.args[1][0]['restaurant_id'], '123')

    def test_one_shot_is_consumed_before_dispatch(self):
        config = task()
        spec = {'id': 'fixture', 'task_id': task_id(config), 'repeat': 'Once', 'duration': 1, 'dry_run': True}
        save_data('tasks.json', [config])
        save_data('schedules.json', [spec])
        manager = TaskManager(Mock())
        manager.start = Mock(
            side_effect=lambda *a, **k: self.assertEqual(load_data('schedules.json', []), [])
        )
        manager.dispatch(spec)
        manager.start.assert_called_once()

    def test_restore_skips_missed_once_schedule(self):
        spec = {
            'id': 'fixture',
            'task_id': 'fixture',
            'repeat': 'Once',
            'time': '18:00',
            'at': '2000-01-01T18:00:00-05:00',
            'duration': 1,
            'dry_run': True,
        }
        save_data('schedules.json', [spec])
        manager = TaskManager(Mock())
        with patch('client.scheduling.BackgroundScheduler') as scheduler:
            manager.ensure_scheduler()
            scheduler.return_value.add_job.assert_not_called()

    def test_schedule_file_persists_without_credentials(self):
        spec = {
            'id': 'fixture',
            'task_id': task_id(task()),
            'repeat': 'Weekly',
            'weekday': 'fri',
            'time': '18:00',
            'duration': 120,
            'dry_run': True,
        }
        manager = TaskManager(Mock())
        with patch('client.scheduling.BackgroundScheduler'):
            manager.add(spec)
        self.assertEqual(load_data('schedules.json', []), [spec])
        self.assertNotIn('fixture-account-token', (self.root / 'schedules.json').read_text())
