"""Team work activity observed through the real loopback server and its in-container client."""

from __future__ import annotations

import http.client
import json
import runpy
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local import activity as local_activity
from local.http import audit as http_audit
from local.http import server
from local.install.automatic import AutomaticAssistantUpdater

TOKEN = "a" * 64
CONNECTION = http.client.HTTPConnection


class ActivityCounterTests(unittest.TestCase):
    def test_nested_and_failed_work_returns_to_idle(self) -> None:
        activity = local_activity.Activity()
        self.assertEqual(activity.state(), "idle")
        with activity.working():
            with activity.working():
                self.assertEqual(activity.state(), "busy")
            self.assertEqual(activity.state(), "busy")
        with self.assertRaises(RuntimeError), activity.working():
            raise RuntimeError
        self.assertEqual(activity.state(), "idle")

    def test_an_automatic_assistant_update_is_busy_while_it_installs(self) -> None:
        activity = local_activity.Activity()
        observed: list[str] = []
        binding = SimpleNamespace(
            provenance="published",
            team_id="team_1",
            assistant_id="hello-world",
            binding_digest=f"sha256:{'1' * 64}",
            resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'a' * 64}"},
        )
        controller = SimpleNamespace(
            developers=SimpleNamespace(
                latest=lambda _digest: {
                    "assistant_id": "hello-world",
                    "assistant_version": "0.2.0",
                    "source_digest": f"sha256:{'9' * 64}",
                }
            ),
            registry=SimpleNamespace(bindings=lambda: (binding,)),
            install_publication=lambda *_args, **_options: observed.append(activity.state()),
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )
        self.assertTrue(AutomaticAssistantUpdater(controller, activity=activity).run_once())
        self.assertEqual((observed, activity.state()), (["busy"], "idle"))


class LoopbackActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

        def complete(**_body: object) -> dict[str, object]:
            self.entered.set()
            self.release.wait(5)
            return {"connected": True}

        controller = SimpleNamespace(
            chat_turn_service=SimpleNamespace(complete_cloudflare_oauth_callback=complete),
        )
        audit = mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32)
        audit.start()
        self.addCleanup(audit.stop)
        self.server = server.BoundedServer(("127.0.0.1", 0), server.Handler, controller, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.release.set)

    def client(self) -> tuple[int, str]:
        port = self.server.server_address[1]

        def connection(host: str, _port: int, **options: object) -> http.client.HTTPConnection:
            return CONNECTION(host, port, **options)

        with (
            mock.patch.object(Path, "read_text", return_value=TOKEN),
            mock.patch.object(local_activity.http.client, "HTTPConnection", side_effect=connection),
            mock.patch("builtins.print") as printed,
        ):
            result = local_activity.main()
        return result, printed.call_args.args[0] if printed.called else ""

    def callback(self) -> None:
        body = json.dumps({"state": "s", "claim": "c", "session_binding": "b"}).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(
            "POST",
            "/v1/oauth/cloudflare/callback",
            body=body,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        connection.getresponse().read()
        connection.close()

    def test_the_client_reports_busy_only_while_a_mutating_request_is_in_flight(self) -> None:
        self.assertEqual(self.client(), (0, "idle"))
        request = threading.Thread(target=self.callback)
        request.start()
        self.assertTrue(self.entered.wait(5))
        self.assertEqual(self.client(), (0, "busy"))
        self.release.set()
        request.join(5)
        self.assertEqual(self.client(), (0, "idle"))

    def test_the_route_requires_the_machine_bearer_and_the_client_fails_closed(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", "/v1/activity")
        self.assertEqual(connection.getresponse().status, 401)
        connection.close()
        with mock.patch.object(Path, "read_text", return_value="short"):
            self.assertEqual(local_activity.main(), 1)
        with mock.patch.object(Path, "read_text", side_effect=OSError):
            self.assertEqual(local_activity.main(), 1)
            with self.assertRaises(SystemExit) as exit_:
                runpy.run_module("local.activity", run_name="__main__")
        self.assertEqual(exit_.exception.code, 1)
        empty = mock.Mock()
        empty.getresponse.return_value.status = 200
        empty.getresponse.return_value.getheader.side_effect = lambda name, default=None: (
            "application/json" if name == "Content-Type" else "0"
        )
        with (
            mock.patch.object(Path, "read_text", return_value=TOKEN),
            mock.patch.object(local_activity.http.client, "HTTPConnection", return_value=empty),
        ):
            self.assertEqual(local_activity.main(), 1)
        empty.close.assert_called_once_with()
        refused = mock.Mock()
        refused.request.side_effect = ConnectionRefusedError
        with (
            mock.patch.object(Path, "read_text", return_value=TOKEN),
            mock.patch.object(local_activity.http.client, "HTTPConnection", return_value=refused),
        ):
            self.assertEqual(local_activity.main(), 1)
        refused.close.assert_called_once_with()
        for payload in ({"state": "unknown"}, {"state": "idle", "extra": 1}, ["idle"]):
            with (
                self.subTest(payload=payload),
                mock.patch.object(
                    server.Handler, "_machine_read_route", return_value=(200, payload, "activity", None, None)
                ),
            ):
                self.assertEqual(self.client(), (1, ""))


if __name__ == "__main__":
    unittest.main()
