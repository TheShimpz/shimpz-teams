"""Team work activity observed through the real loopback server and its in-container client."""

from __future__ import annotations

import contextlib
import http.client
import json
import runpy
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local import activity as local_activity
from local import authority as local_authority
from local.http import audit as http_audit
from local.http import server
from local.install.automatic import AutomaticAssistantUpdater

TOKEN = "a" * 64
CONNECTION = http.client.HTTPConnection


class SettledActivity(local_activity.Activity):
    """Lets a test wait for the exact moment the in-flight count returns to zero.

    The handler sends its response inside its counted work, so a client can read the reply before the count drops.
    """

    def __init__(self, **options: object) -> None:
        super().__init__(**options)
        self._settled = threading.Condition()

    @contextlib.contextmanager
    def working(self) -> Iterator[None]:
        try:
            with super().working():
                yield
        finally:
            with self._settled:
                self._settled.notify_all()

    def wait_settled(self) -> bool:
        with self._settled:
            return self._settled.wait_for(lambda: not self._active, timeout=5)


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
            local_snapshot_collector=SimpleNamespace(collect=lambda: None),
        )
        self.assertTrue(AutomaticAssistantUpdater(controller, activity=activity).run_once())
        self.assertEqual((observed, activity.state()), (["busy"], "idle"))


class SupervisorQuietWindowTests(unittest.TestCase):
    """Supervisor mutations and chat keep Team busy for the quiet window; machine calls and reads never do."""

    def setUp(self) -> None:
        self.now = 1_000.0
        controller = SimpleNamespace(
            health=lambda: {"status": "ok"},
            list_teams=lambda: {"teams": []},
            create_team=lambda team_id, team_name: {"team_id": team_id, "team_name": team_name},
            chat_turn_service=SimpleNamespace(
                claim_routine_run=lambda: None,
                next_routine_due=lambda: None,
                routine_notices=lambda: {"notices": []},
            ),
        )
        audit = mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32)
        audit.start()
        self.addCleanup(audit.stop)
        verify = mock.patch.object(
            server.local_authority,
            "verify",
            return_value=local_authority.Evidence(
                supervisor_id="a" * 32,
                authority_kind="session",
                authority_digest="b" * 64,
                assertion_id="c" * 32,
                expires_at=2_200_000_015,
            ),
        )
        verify.start()
        self.addCleanup(verify.stop)
        self.server = server.BoundedServer(("127.0.0.1", 0), server.Handler, controller, TOKEN)
        self.server.activity = SettledActivity(clock=lambda: self.now)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method: str, path: str, body: dict[str, object] | None = None) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=headers)
        status = connection.getresponse().status
        connection.close()
        # The response leaves before the handler's in-flight count drops; wait for it so only the window remains.
        self.assertTrue(self.server.activity.wait_settled())
        return status

    def create_team(self) -> int:
        return self.request("POST", "/v1/teams/team_1/create", {"team_name": "Team"})

    def test_a_supervisor_mutation_keeps_team_busy_only_for_the_quiet_window(self) -> None:
        self.assertEqual(self.server.activity.state(), "idle")
        self.assertEqual(self.create_team(), 200)
        self.assertEqual(self.server.activity.state(), "busy")
        self.now += local_activity.QUIET_SECONDS - 1
        self.assertEqual(self.server.activity.state(), "busy")
        self.now += 1
        self.assertEqual(self.server.activity.state(), "idle")

    def test_a_later_supervisor_mutation_restarts_the_window(self) -> None:
        self.assertEqual(self.create_team(), 200)
        self.now += local_activity.QUIET_SECONDS - 10
        self.assertEqual(self.create_team(), 200)
        self.now += local_activity.QUIET_SECONDS - 1
        self.assertEqual(self.server.activity.state(), "busy")

    def test_machine_calls_supervisor_reads_and_refused_authority_stay_idle(self) -> None:
        self.assertEqual(self.request("GET", "/healthz"), 200)
        self.assertEqual(self.request("GET", "/v1/activity"), 200)
        self.assertEqual(self.request("POST", "/v1/routines/claim", {}), 200)
        self.assertEqual(self.request("GET", "/v1/routines/notices"), 200)
        self.assertEqual(self.request("GET", "/v1/teams"), 200)
        with mock.patch.object(server.local_authority, "verify", side_effect=local_authority.SupervisorDeniedError):
            self.assertEqual(self.create_team(), 403)
        self.assertEqual(self.server.activity.state(), "idle")

    def test_a_restarted_team_forgets_the_quiet_window(self) -> None:
        self.assertEqual(self.create_team(), 200)
        self.assertEqual(self.server.activity.state(), "busy")
        restarted = server.BoundedServer(("127.0.0.1", 0), server.Handler, self.server.controller, TOKEN)
        self.addCleanup(restarted.server_close)
        self.assertEqual(restarted.activity.state(), "idle")


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
        self.server.activity = SettledActivity()
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
        self.assertTrue(self.server.activity.wait_settled())
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
