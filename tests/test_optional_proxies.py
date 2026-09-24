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
    def client(self, proxies):
        data = {
            "tasks": [{"restaurant_id": "test-venue"}],
            "proxies": proxies,
            "info": {
                "capsolver_key": "",
                "capmonster_key": "",
                "discord_webhook": "",
            },
        }
        return load_functions(
            "client/resygrabber.py",
            {"start_tasks", "run_task_with_timeout", "get_random_proxy"},
            TASKS_FILE="tasks", PROXIES_FILE="proxies", INFO_FILE="info",
            load_data=lambda name, default: data.get(name, default),
            click=types.SimpleNamespace(echo=Mock()),
            input=Mock(),
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
        client.click.echo.assert_called_once_with(
            "No proxies configured; using the default network connection."
        )

    def test_start_tasks_preserves_configured_proxies(self):
        proxies = ["proxy.example:8080:test-user:test-password"]
        client = self.client(proxies)
        client.start_tasks()
        self.assertEqual(client.run_tasks_concurrently.call_args.args[3], proxies)
        client.click.echo.assert_not_called()

    def test_scheduled_task_passes_empty_proxy_list(self):
        client = self.client([])
        client.run_task_with_timeout(0, 0, "test-job")
        client.run_tasks_concurrently.assert_called_once_with(
            [{"restaurant_id": "test-venue"}], "", "", [], ""
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

    def check_worker(self, proxies, expected):
        response = types.SimpleNamespace(status_code=503, text="offline fixture")
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
            "", "(1) Failed to get availability for restaurant test-venue - offline fixture - 503"
        )


if __name__ == "__main__":
    unittest.main()
