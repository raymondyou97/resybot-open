import io
import json
import os
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from client import reservation_plan as plan, resygrabber
from client.config_store import load_data, save_data
from client.fees import FeePolicyError, validate_policy
from client.scheduling import TaskManager, task_id
from client.time_window import time_window
from scripts.check_secrets import scan
from support import OfflineTest, task


PLAN = """# Public target only
[coop]
name = Double Chicken Please
url = https://resy.com/cities/new-york-ny/venues/double-chicken-please?date=2026-09-24&seats=2
venue_id = 42534
party_size = 2
start_date = 2099-09-26
end_date = 2099-10-06
start_time = 17:00
end_time = 20:00
timezone = America/New_York
"""


class ReservationPlanTests(OfflineTest):
    def write_plan(self, content=PLAN):
        plan.PLAN_PATH.write_text(content, encoding='utf-8')

    def account(self):
        config = task()
        account = {key: config[key] for key in ('account_name', 'auth_token', 'payment_id')}
        save_data('accounts.json', [account])
        return account

    def test_plan_loads_dates_party_and_exact_time_independently_of_url_query(self):
        self.write_plan()
        goal = plan.load_goals()[0]
        self.assertEqual(goal['restaurant_id'], '42534')
        self.assertEqual(goal['party_sz'], 2)
        self.assertEqual((goal['start_date'], goal['end_date']), ('2099-09-26', '2099-10-06'))
        self.assertEqual(time_window(goal), (17 * 60, 20 * 60))
        self.assertEqual(goal['timezone'], 'America/New_York')
        self.assertEqual(goal['campaign_id'], 'coop')
        self.assertNotIn('auth_token', goal)

    def test_canonical_venue_url_without_query_parameters_is_valid(self):
        self.write_plan(PLAN.replace('?date=2026-09-24&seats=2', ''))
        goal = plan.load_goals()[0]
        self.assertEqual(goal['restaurant_id'], '42534')
        self.assertEqual(goal['start_date'], '2099-09-26')

    def test_two_restaurants_are_independent_goals_without_date_spacing(self):
        second = (
            PLAN.replace('[coop]', '[4-charles-prime-rib]')
            .replace('Double Chicken Please', '4 Charles Prime Rib')
            .replace('double-chicken-please', '4-charles-prime-rib')
            .replace('venue_id = 42534', 'venue_id = 834')
            .replace('end_time = 20:00', 'end_time = 19:30')
        )
        self.write_plan(PLAN + '\n' + second)
        account = self.account()
        for goal in plan.load_goals():
            plan.save_goal_settings(goal, account, task(max_total_charge='any', max_cancellation_fee='any'))
        first, second = plan.load_tasks()
        self.assertNotEqual(first['campaign_id'], second['campaign_id'])
        self.assertEqual(time_window(second), (17 * 60, 19 * 60 + 30))
        claim = self.state.claim(first, '2099-09-26', '18:00')
        self.state.submitted(claim)
        self.state.confirmed(claim, 'fixture-stable-reference')
        self.assertTrue(self.state.blocked(first))
        self.assertFalse(self.state.blocked(second))
        self.assertTrue(self.state.claim(second, '2099-09-26', '18:00'))

    def test_single_local_account_is_bound_in_memory_without_rewriting_files(self):
        self.write_plan()
        account = self.account()
        save_data('tasks.json', [task(restaurant_id='96798')])
        before = plan.PLAN_PATH.read_bytes()
        loaded = plan.load_tasks()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]['auth_token'], account['auth_token'])
        self.assertEqual(loaded[0]['restaurant_id'], '42534')
        with self.assertRaises(FeePolicyError):
            validate_policy(loaded[0])
        self.assertEqual(plan.PLAN_PATH.read_bytes(), before)
        self.assertEqual(load_data('tasks.json', [])[0]['restaurant_id'], '96798')
        self.assertFalse((self.root / plan.SETTINGS_FILE).exists())

    def test_private_settings_exclude_credentials_and_public_target_fields(self):
        self.write_plan()
        account = self.account()
        goal = plan.load_goals()[0]
        plan.save_goal_settings(goal, account, task())
        settings = load_data(plan.SETTINGS_FILE, {})
        self.assertNotIn('auth_token', json.dumps(settings))
        self.assertNotIn('payment_id', json.dumps(settings))
        self.assertNotIn('restaurant_id', settings['coop'])
        self.assertEqual((self.root / plan.SETTINGS_FILE).stat().st_mode & 0o777, 0o600)
        validate_policy(plan.load_tasks()[0])
        self.assertNotIn(account['auth_token'], plan.PLAN_PATH.read_text())
        self.assertIn('private/runtime file', scan('client/reservation-settings.json', b'{}'))

    def test_menu_saves_only_private_settings(self):
        self.write_plan()
        self.account()
        original = plan.PLAN_PATH.read_bytes()

        def approve(target):
            target.update({key: task()[key] for key in plan.POLICY_FIELDS})

        with (
            patch.object(resygrabber, 'choose', side_effect=['Configure account and fee limits', 0, 0]),
            patch.object(resygrabber, 'policy', side_effect=approve),
            redirect_stdout(io.StringIO()),
        ):
            resygrabber.show_tasks()
        self.assertEqual(plan.PLAN_PATH.read_bytes(), original)
        validate_policy(plan.load_tasks()[0])
        self.assertFalse((self.root / 'tasks.json').exists())

    def test_live_public_target_needs_its_own_fee_approval(self):
        self.write_plan()
        self.account()
        save_data('tasks.json', [task()])
        with patch.object(resygrabber, 'MANAGER') as manager, self.assertRaises(FeePolicyError):
            resygrabber.start_tasks(dry_run=False)
        manager.start.assert_not_called()

    def test_legacy_hour_windows_are_preserved(self):
        self.assertEqual(time_window(task(start_time=17, end_time=20)), (17 * 60, 20 * 60 + 59))
        for start, end in [('17:60', '20:00'), ('17:00', '24:00'), (True, 20), (17.5, 20)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                time_window(task(start_time=start, end_time=end))

    def test_target_edit_requires_new_fee_approval(self):
        self.write_plan()
        account = self.account()
        plan.save_goal_settings(plan.load_goals()[0], account, task())
        self.write_plan(PLAN.replace('end_time = 20:00', 'end_time = 19:30'))
        with self.assertRaises(FeePolicyError):
            validate_policy(plan.load_tasks()[0])

    def test_multiple_accounts_require_explicit_selection(self):
        self.write_plan()
        account = self.account()
        second = {**account, 'account_name': 'second', 'auth_token': 'second-fixture-token'}
        save_data('accounts.json', [account, second])
        with self.assertRaises(ValueError):
            plan.load_tasks()
        plan.save_goal_settings(plan.load_goals()[0], second, task())
        self.assertEqual(plan.load_tasks()[0]['account_name'], 'second')

    def test_invalid_plan_never_falls_back_to_saved_tasks(self):
        save_data('tasks.json', [task()])
        cases = [
            PLAN + 'auth_token = fixture\n',
            PLAN + 'unexpected_field = 1\n',
            PLAN.replace('party_size = 2', 'party_size = 0'),
            PLAN.replace('end_time = 20:00', 'end_time = 16:59'),
            PLAN.replace('start_time = 17:00', 'start_time = 17'),
            PLAN.replace('2099-10-06', '2099-12-06'),
            PLAN.replace('America/New_York', 'invalid-zone'),
            PLAN.replace('https://resy.com/', 'https://example.invalid/'),
            PLAN.replace('&seats=2', '&auth_token=fixture'),
            '[DEFAULT]\nauth_token = fixture\n' + PLAN,
            PLAN + '\n[coop]\n',
        ]
        for index, content in enumerate(cases):
            self.write_plan(content)
            with self.subTest(case=index), self.assertRaises(ValueError):
                plan.load_tasks()

    def test_absent_plan_uses_legacy_tasks_but_empty_plan_disables_them(self):
        save_data('tasks.json', [task()])
        self.assertEqual(plan.load_tasks(), [task()])
        self.write_plan('# No active goals\n')
        self.assertEqual(plan.load_tasks(), [])

    def test_check_does_not_need_credentials_or_depend_on_launch_directory(self):
        self.write_plan()
        output = io.StringIO()
        previous = os.getcwd()
        try:
            os.chdir(self.root)
            with redirect_stdout(output):
                resygrabber.main(['--check'])
        finally:
            os.chdir(previous)
        self.assertIn('17:00–20:00', output.getvalue())
        self.assertIn('1 task(s)', output.getvalue())
        self.assertFalse((self.root / 'accounts.json').exists())

    def test_start_uses_only_public_targets_and_never_old_test_tasks(self):
        self.write_plan()
        self.account()
        save_data('tasks.json', [task(restaurant_id='96798')])
        with patch.object(resygrabber, 'MANAGER') as manager, redirect_stdout(io.StringIO()):
            resygrabber.start_tasks(dry_run=True)
        manager.start.assert_called_once()
        self.assertEqual(manager.start.call_args.args[1][0]['restaurant_id'], '42534')

    def test_scheduled_dispatch_reloads_plan_and_skips_changed_targets(self):
        self.write_plan()
        self.account()
        config = plan.load_tasks()[0]
        manager = TaskManager(Mock())
        manager.start = Mock()
        spec = {
            'id': 'fixture',
            'task_id': task_id(config),
            'repeat': 'Daily',
            'duration': 1,
            'dry_run': True,
        }
        manager.dispatch(spec)
        manager.start.assert_called_once()
        manager.start.reset_mock()
        self.write_plan(PLAN.replace('2099-10-06', '2099-10-05'))
        with redirect_stdout(io.StringIO()):
            manager.dispatch(spec)
        manager.start.assert_not_called()

    def test_secret_scanner_rejects_private_fields_in_public_plan(self):
        self.assertEqual(scan('reservations.txt', PLAN.encode()), [])
        self.assertIn(
            'private setting in public reservation plan', scan('reservations.txt', b'auth_token = fixture')
        )
