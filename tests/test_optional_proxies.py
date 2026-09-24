"""Offline regression tests; never import the interactive client's scheduler."""

import ast
from pathlib import Path
import random
import types
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def load_functions(relative_path, names, **dependencies):
    """Load selected definitions without imports, startup code, or account files."""
    tree = ast.parse((ROOT / relative_path).read_text())
    definitions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in definitions} != set(names):
        raise AssertionError("Requested function definitions were not found")
    module = ast.Module(body=definitions, type_ignores=[])
    namespace = dict(dependencies)
    exec(compile(module, relative_path, "exec"), namespace)
    return types.SimpleNamespace(**namespace)


class OptionalProxiesTests(unittest.TestCase):
    def client(self, proxies, info=None, tasks=None):
        data = {
            "tasks": [{"restaurant_id": "test-venue"}] if tasks is None else tasks,
            "proxies": proxies,
        }
        if info is not None:
            data["info"] = info
        return load_functions(
            "client/resygrabber.py",
            {"start_tasks", "run_task_with_timeout", "get_random_proxy"},
            TASKS_FILE="tasks", PROXIES_FILE="proxies", INFO_FILE="info",
            load_data=lambda name, default: data.get(name, default),
            click=types.SimpleNamespace(echo=Mock()),
            input=Mock(),
            print=Mock(),
            random=random,
            run_tasks_concurrently=Mock(),
            threading=types.SimpleNamespace(current_thread=Mock()),
            time=types.SimpleNamespace(time=Mock(return_value=0), sleep=Mock()),
            running_tasks={},
        )

    def test_start_tasks_with_no_proxies(self):
        client = self.client([])
        client.start_tasks()
        client.run_tasks_concurrently.assert_called_once_with(
            [{"restaurant_id": "test-venue"}], "", "", [], ""
        )
        client.input.assert_called_once()
        client.click.echo.assert_any_call(
            "No proxies configured; using the default network connection."
        )
        client.click.echo.assert_any_call("Starting 1 reservation task(s)...")

    def test_start_tasks_preserves_configured_proxies(self):
        proxies = ["proxy.example:8080:test-user:test-password"]
        client = self.client(proxies)
        client.start_tasks()
        self.assertEqual(client.run_tasks_concurrently.call_args.args[3], proxies)
        self.assertNotIn(
            (("No proxies configured; using the default network connection.",), {}),
            client.click.echo.call_args_list,
        )

    def test_scheduled_task_passes_empty_proxy_list(self):
        client = self.client([])
        client.run_task_with_timeout(0, 0, "test-job")
        client.run_tasks_concurrently.assert_called_once_with(
            [{"restaurant_id": "test-venue"}], "", "", [], ""
        )

    def test_empty_and_partial_info_are_optional(self):
        for info in [{}, {"capsolver_key": ""}, {"discord_webhook": ""}]:
            with self.subTest(info=info):
                for scheduled in [False, True]:
                    client = self.client([], info=info)
                    if scheduled:
                        client.run_task_with_timeout(0, 0, "test-job")
                    else:
                        client.start_tasks()
                    client.run_tasks_concurrently.assert_called_once_with(
                        [{"restaurant_id": "test-venue"}], "", "", [], ""
                    )

    def test_configured_info_is_preserved(self):
        info = {
            "capsolver_key": "offline-solver-fixture",
            "capmonster_key": "offline-second-solver-fixture",
            "discord_webhook": "https://example.invalid/offline-webhook",
        }
        for scheduled in [False, True]:
            client = self.client([], info=info)
            if scheduled:
                client.run_task_with_timeout(0, 0, "test-job")
            else:
                client.start_tasks()
            client.run_tasks_concurrently.assert_called_once_with(
                [{"restaurant_id": "test-venue"}], info["capsolver_key"],
                info["capmonster_key"], [], info["discord_webhook"]
            )

    def test_no_tasks_message_waits_before_menu_redraw(self):
        client = self.client([], tasks=[])
        client.start_tasks()
        client.run_tasks_concurrently.assert_not_called()
        client.input.assert_called_once()

    def test_startup_exception_waits_before_menu_redraw(self):
        client = self.client([])
        client.run_tasks_concurrently.side_effect = RuntimeError("offline failure")
        client.start_tasks()
        client.print.assert_called_once_with("Error starting tasks: offline failure")
        client.input.assert_called_once()

    def test_notification_without_webhook_does_not_send_or_print_payload(self):
        requests = types.SimpleNamespace(post=Mock())
        worker = load_functions(
            "client/task_executor.py", {"send_discord_notification"},
            requests=requests, print=Mock(),
        )
        for webhook in [None, ""]:
            worker.send_discord_notification(webhook, "private fixture payload")
        requests.post.assert_not_called()
        self.assertNotIn("private fixture payload", str(worker.print.call_args_list))

    def test_notification_prints_only_safe_summary(self):
        requests = types.SimpleNamespace(post=Mock())
        worker = load_functions(
            "client/task_executor.py", {"send_discord_notification"},
            requests=requests, print=Mock(),
        )
        summary = 'Availability check failed (HTTP 403); no booking was submitted by this task.'
        worker.send_discord_notification('', 'private fixture payload', summary=summary)
        worker.print.assert_any_call(summary)
        self.assertNotIn('private fixture payload', str(worker.print.call_args_list))
        requests.post.assert_not_called()

    def test_notification_preserves_configured_webhook(self):
        requests = types.SimpleNamespace(post=Mock())
        worker = load_functions(
            "client/task_executor.py", {"send_discord_notification"},
            requests=requests, print=Mock(),
        )
        worker.send_discord_notification("https://example.invalid/offline-webhook", "fixture")
        requests.post.assert_called_once_with(
            "https://example.invalid/offline-webhook", json={"content": "fixture"}
        )

    def test_reservation_management_without_proxies(self):
        client = self.client([])
        self.assertEqual(client.get_random_proxy(), {})

    def test_reservation_management_preserves_configured_proxy(self):
        client = self.client(["proxy.example:8080:test-user:test-password"])
        self.assertEqual(client.get_random_proxy(), {
            "http": "http://test-user:test-password@proxy.example:8080",
            "https": "http://test-user:test-password@proxy.example:8080",
        })

    def test_worker_uses_default_connection_without_proxies(self):
        self.check_worker([], {})

    def test_worker_preserves_configured_proxy(self):
        self.check_worker(["proxy.example:8080:test-user:test-password"], {
            "http": "http://test-user:test-password@proxy.example:8080",
            "https": "http://test-user:test-password@proxy.example:8080",
        })

    def test_worker_reports_auth_access_and_rate_limit_errors(self):
        for status in [401, 403, 429]:
            with self.subTest(status=status):
                self.check_worker([], {}, status=status)

    def check_worker(self, proxies, expected, status=503):
        response = types.SimpleNamespace(status_code=status, text="offline fixture")
        requests = types.SimpleNamespace(get=Mock(return_value=response))
        notify = Mock()
        worker = load_functions(
            "client/task_executor.py", {"execute_task", "format_proxy"},
            random=random, requests=requests, send_discord_notification=notify,
        )
        task = {
            "auth_token": "offline-fixture", "payment_id": 0,
            "restaurant_id": "test-venue", "party_sz": 2,
            "start_date": "2030-01-01", "end_date": "2030-01-01",
            "start_time": 18, "end_time": 19, "delay": 60000,
        }
        worker.execute_task(task, "", "", proxies, "")
        requests.get.assert_called_once()
        self.assertEqual(requests.get.call_args.kwargs["proxies"], expected)
        notify.assert_called_once_with(
            "", f"(1) Failed to get availability for restaurant test-venue - offline fixture - {status}",
            summary=f"Availability check failed (HTTP {status}); no booking was submitted by this task.",
        )


if __name__ == "__main__":
    unittest.main()
