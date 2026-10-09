"""Edges of Team-made provider calls: each profile's egress route, admission refusals, and transport bounds."""

import base64
import struct
import threading
import time
import types
import unittest
from unittest import mock

import hosted_assistant_fixture as harness

from action import frames as action_frames
from action import provider
from local import app as local_app

hosted_runtime = harness.hosted_assistants

HOST = "api.example.com"


def _action() -> types.SimpleNamespace:
    return types.SimpleNamespace(stored_inputs=(), integrations=(), human_requests=())


def _private() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        operation_id="6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
        stored_inputs={},
        integrations={},
        transcript=types.SimpleNamespace(responses=()),
    )


class EgressRouteTests(unittest.TestCase):
    """Each profile reads the Assistant's own egress policy only when the attempt makes its first call."""

    def test_the_hosted_route_reads_the_admitted_policy_and_refuses_an_unavailable_one(self) -> None:
        contract = types.SimpleNamespace(stored_inputs={}, integrations={}, actions={"run": _action()})
        request = types.SimpleNamespace(team_id="team_1", assistant_id="example", action="run", contract=contract)
        for admitted, expected in ((None, ("", frozenset())), (("token", [HOST]), ("token", frozenset({HOST})))):
            store = types.SimpleNamespace(admitted=mock.Mock(return_value=admitted))
            with self.subTest(admitted=admitted), mock.patch.object(
                hosted_runtime.assistant_lifecycle, "_egress_store", return_value=store
            ):
                self.assertEqual(hosted_runtime._provider_broker(request, _private())._scope.route(), expected)
        drift = hosted_runtime.egress_policy.EgressPolicyError("drift")
        failing = types.SimpleNamespace(admitted=mock.Mock(side_effect=drift))
        with (
            mock.patch.object(hosted_runtime.assistant_lifecycle, "_egress_store", return_value=failing),
            self.assertRaises(hosted_runtime.action_provider.CallRefusedError) as refused,
        ):
            hosted_runtime._provider_broker(request, _private())._scope.route()
        self.assertEqual((refused.exception.code, refused.exception.reason), ("unavailable", "egress-policy"))
        with mock.patch.object(hosted_runtime.audit, "log") as log:
            hosted_runtime._provider_broker(request, _private())._scope.audit(
                {"phase": "refused", "call": 1, "error": "refused", "reason": "host"}
            )
        self.assertEqual((log.call_args.args[0], log.call_args.kwargs["result"]), ("assistant_provider_call", "denied"))

    def test_the_local_route_reads_the_assistant_token_and_refuses_an_unavailable_one(self) -> None:
        spec = types.SimpleNamespace(assistant_id="example", stored_inputs={}, integrations={}, allowed_hosts=(HOST,))
        for token, expected in ((None, ("", frozenset())), ("token", ("token", frozenset({HOST})))):
            controller = types.SimpleNamespace(
                assistant_lifecycle=types.SimpleNamespace(_egress_token=mock.Mock(return_value=token))
            )
            broker = local_app.LocalController._provider_broker(
                controller, "team_1", spec, "run", _action(), _private()
            )
            with self.subTest(token=token):
                self.assertEqual(broker._scope.route(), expected)
        problem = local_app.ApiProblem(503, "unavailable", code="egress-policy-unavailable")
        controller = types.SimpleNamespace(
            assistant_lifecycle=types.SimpleNamespace(_egress_token=mock.Mock(side_effect=problem))
        )
        broker = local_app.LocalController._provider_broker(controller, "team_1", spec, "run", _action(), _private())
        with self.assertRaises(provider.CallRefusedError):
            broker._scope.route()
        with mock.patch.object(local_app.local_audit, "record_request") as record:
            broker._scope.audit({"phase": "outcome", "call": 1, "status": 200})
        self.assertEqual(record.call_args.kwargs["result"], "ok")
        self.assertNotIn("token", record.call_args.kwargs["detail"])


class AdmissionEdgeTests(unittest.TestCase):
    def test_refuses_an_invalid_port_malformed_headers_and_oversized_bodies(self) -> None:
        hosts = frozenset({HOST})
        frame = {"type": "fetch", "method": "POST", "url": f"https://{HOST}/", "headers": []}
        oversized = base64.b64encode(b"x" * (provider.MAX_REQUEST_BYTES + 1)).decode()
        for changed in (
            {"url": f"https://{HOST}:99999/"},
            {"headers": {"accept": "*/*"}},
            {"headers": [["x-a", "1"]] * (provider.MAX_HEADERS + 1)},
            {"body": 7},
            {"body": oversized},
        ):
            with self.subTest(changed=str(changed)[:40]), self.assertRaises(provider.CallRefusedError):
                provider._parse({**frame, **changed}, hosts)

    def test_a_credential_for_another_host_is_not_placed(self) -> None:
        call = provider._Call("GET", HOST, "/", (), None, None)
        other = provider.Credential("stored-input:o", "other.example.com", "x-key", None, "value", ("value",))
        self.assertEqual(provider._inject(call, (other,)), call)


class TransportEdgeTests(unittest.TestCase):
    def test_capacity_waits_end_at_the_deadline_or_a_stop_that_arrived_while_waiting(self) -> None:
        with self.assertRaises(provider.CallRefusedError) as expired:
            provider._acquire(time.monotonic() - 1, lambda: False)
        self.assertEqual(expired.exception.code, "unavailable")
        answers = iter((False, True))
        free = provider._CAPACITY._value
        with self.assertRaises(provider.CallRefusedError) as stopped:
            provider._acquire(time.monotonic() + 5, lambda: next(answers))
        self.assertEqual(stopped.exception.code, "refused")
        self.assertEqual(provider._CAPACITY._value, free)

    def test_a_call_waiting_for_capacity_gives_up_at_its_deadline(self) -> None:
        held = 0
        while provider._CAPACITY.acquire(blocking=False):
            held += 1
        try:
            with mock.patch.object(provider, "_STOP_POLL_SECONDS", 0.01), self.assertRaises(
                provider.CallRefusedError
            ) as refused:
                provider._acquire(time.monotonic() + 0.05, lambda: False)
        finally:
            for _ in range(held):
                provider._CAPACITY.release()
        self.assertEqual((refused.exception.code, refused.exception.reason), ("unavailable", "deadline"))

    def test_a_call_whose_deadline_already_passed_is_never_sent(self) -> None:
        call = provider._Call("GET", HOST, "/", (), None, None)
        with (
            mock.patch.object(provider.http.client, "HTTPSConnection") as connection,
            self.assertRaises(provider.CallRefusedError) as refused,
        ):
            provider._transport(call, "token", lambda: False, time.monotonic() - 1)
        self.assertEqual(refused.exception.code, "unavailable")
        connection.assert_not_called()

    def test_a_response_whose_deadline_expired_while_it_was_read_is_refused(self) -> None:
        response = mock.Mock(status=200, length=None)
        response.getheaders.return_value = []
        response.getheader.return_value = "identity"
        response.read1.side_effect = [b"{}", b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        call = provider._Call("GET", HOST, "/", (), None, None)
        with self.assertRaises(provider.CallRefusedError) as refused:
            provider._exchange(connection, call, types.SimpleNamespace(expired=True))
        self.assertEqual((refused.exception.code, refused.exception.reason), ("failed", "timeout"))

    def test_the_stop_watcher_keeps_waiting_until_the_turn_is_stopped(self) -> None:
        stopped = threading.Event()
        checks: list[bool] = []

        def observed() -> bool:
            checks.append(stopped.is_set())
            if len(checks) == 2:
                stopped.set()
            return stopped.is_set() and len(checks) > 2

        guard = mock.Mock()
        with mock.patch.object(provider, "_STOP_POLL_SECONDS", 0.01), provider._Observing(guard, observed):
            deadline = time.monotonic() + 5
            while not guard.expire.called and time.monotonic() < deadline:
                time.sleep(0.01)
        guard.expire.assert_called_once()


class FrameEdgeTests(unittest.TestCase):
    def test_a_provider_call_line_that_is_not_utf8_is_a_frame_fault(self) -> None:
        reader = action_frames._FrameReader(1024)
        line = b"\xff\n"
        reader.feed(struct.pack(">BxxxL", 1, len(line)) + line)
        with self.assertRaisesRegex(ValueError, "invalid Assistant RPC line"):
            reader.next_call()


if __name__ == "__main__":
    unittest.main()
