import io
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from client.control import RunControl
from client import task_executor as worker
from support import OfflineTest, availability, task


class OptionalConfigurationTests(OfflineTest):
    def test_default_connection_and_configured_proxy(self):
        for proxies, expected in [
            ([], {}),
            (
                ['proxy.example:8080:fixture:fixture'],
                {
                    'http': 'http://fixture:fixture@proxy.example:8080',
                    'https': 'http://fixture:fixture@proxy.example:8080',
                },
            ),
        ]:
            with self.subTest(proxies=bool(proxies)):
                control = RunControl()
                control.wait = Mock(return_value=False)
                with (
                    patch.object(
                        worker.requests,
                        'get',
                        return_value=Mock(status_code=200, json=lambda: availability(slots=[])),
                    ) as get,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        worker.execute_task(task(), proxies=proxies, control=control), 'dry-run-complete'
                    )
                    self.assertEqual(get.call_args.kwargs['proxies'], expected)

    def test_notification_never_sends_raw_payload(self):
        output = io.StringIO()
        with (
            patch.object(worker.requests, 'post', return_value=Mock(status_code=204)) as post,
            redirect_stdout(output),
        ):
            worker.send_discord_notification(
                'https://discord.com/api/webhooks/fixture/fixture',
                'private fixture payload',
                summary='Safe outcome',
            )
        self.assertEqual(post.call_args.kwargs['json'], {'content': 'Safe outcome'})
        self.assertNotIn('private fixture payload', output.getvalue())

    def test_missing_and_untrusted_webhooks_do_not_send(self):
        with patch.object(worker.requests, 'post') as post, redirect_stdout(io.StringIO()):
            for url in ['', None, 'http://discord.com/api/webhooks/x', 'https://example.invalid/x']:
                worker.send_discord_notification(url, 'private payload', summary='Safe outcome')
            post.assert_not_called()

    def test_notification_failure_does_not_throw_or_leak(self):
        with patch.object(
            worker.requests, 'post', side_effect=worker.requests.RequestException('secret-url')
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                worker.send_discord_notification(
                    'https://discord.com/api/webhooks/fixture/fixture', summary='Outcome'
                )
            self.assertNotIn('secret-url', output.getvalue())
