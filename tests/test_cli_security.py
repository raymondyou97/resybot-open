import io
from contextlib import redirect_stdout
from unittest.mock import patch

from client import resygrabber
from client.config_store import save_data
from scripts.check_secrets import scan
from support import OfflineTest, task


class CliSecurityTests(OfflineTest):
    def test_import_does_not_start_scheduler(self):
        self.assertIsNone(resygrabber.MANAGER.scheduler)
        self.assertEqual(resygrabber.MANAGER.running, {})

    def test_show_tasks_does_not_print_credentials(self):
        save_data('tasks.json', [task()])
        output = io.StringIO()
        with patch.object(resygrabber, 'choose', return_value='Back'), redirect_stdout(output):
            resygrabber.show_tasks()
        self.assertNotIn(task()['auth_token'], output.getvalue())
        self.assertNotIn('payment_id', output.getvalue())
        self.assertIn('Restaurant 123', output.getvalue())

    def test_live_start_requires_confirmation_and_valid_policy(self):
        save_data('tasks.json', [task()])
        with (
            patch.object(resygrabber, 'MANAGER') as manager,
            patch.object(resygrabber, 'confirm', return_value=False),
        ):
            resygrabber.start_tasks(False)
            manager.start.assert_not_called()
        with (
            patch.object(resygrabber, 'MANAGER') as manager,
            patch.object(resygrabber, 'confirm', return_value=True),
            redirect_stdout(io.StringIO()),
        ):
            resygrabber.start_tasks(False)
            self.assertFalse(manager.start.call_args.kwargs['dry_run'])

    def test_back_from_notifications_does_not_disable_it(self):
        with (
            patch.object(resygrabber, 'choose', return_value='Back'),
            patch.object(resygrabber, 'confirm') as confirm,
            redirect_stdout(io.StringIO()),
        ):
            resygrabber.manage_info()
        confirm.assert_not_called()

    def test_resolving_holds_is_blocked_while_workers_active(self):
        with (
            patch.object(resygrabber, 'MANAGER') as manager,
            patch.object(resygrabber, 'BookingState') as state,
            redirect_stdout(io.StringIO()),
        ):
            manager.statuses.return_value = {'task': 'stopping'}
            resygrabber.manage_holds()
            state.assert_not_called()

    def test_secret_scan_catches_private_files_and_exact_values(self):
        self.assertIn('private/runtime file', scan('client/tasks.json', b'[]'))
        self.assertIn(
            'matches a local credential', scan('client/example.py', b'fixture-value', [b'fixture-value'])
        )
        self.assertIn('private key', scan('example.txt', b'-----BEGIN ' + b'PRIVATE KEY-----'))
        self.assertEqual(scan('README.md', b'No private data'), [])
