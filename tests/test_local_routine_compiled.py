"""A compiled Routine run executes its plan with no model and no model key (ADR-0092 sections 3, 5, and 6)."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import struct
import tempfile
import threading
import time
from http import HTTPStatus
from socket import socketpair
from types import SimpleNamespace
from unittest import mock

from test_local_chat_scope import LOOKUP_INPUT, LOOKUP_RESULT
from test_local_routine_service import KEY, RoutineServiceCase, approval

from action import execution as action_execution
from action import failure as action_failure
from action import human as action_human
from action import journal as action_journal
from inference import client as brain_runtime_client
from local import app as local_app
from local import authority as local_authority
from local.routine import compiled as routine_compiled
from local.routine import diagnostics as local_routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import run as routine_run
from local.routine import store as routine_store
from local.routine import watchdog as routine_watchdog
from routine import cursor as routine_cursor
from routine import plan as routine_plan
from routine import record

ZONE = "c" * 32
ZONES = {"zones": [{"id": ZONE, "name": "example.com"}], "pagination": LOOKUP_RESULT["pagination"]}
RECORDS = {"records": [], "pagination": LOOKUP_RESULT["pagination"]}


class Brain:
    """The Brain: a healthy compiled run must never ask it anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def called(*_args, **_kwargs):
            self.calls.append(name)
            raise AssertionError(f"the Brain was asked to {name}")

        return called


class CompiledRunCase(RoutineServiceCase):
    def compiled(self, directory: str, invoke):
        brain = Brain()
        controller, service = self.service(directory, brain)
        controller.assistant_lifecycle.invoke = invoke
        plan = self.plan(service, ("zones", "list-zones", LOOKUP_INPUT), ("records", "list-dns-records", LOOKUP_INPUT))
        plan["steps"][1]["input"]["zone_id"] = {"kind": "step_output", "step": "zones", "pointer": "/zones/0/id"}
        value = self.routine(service, plan=plan)
        return controller, service, brain, value

    @staticmethod
    def run_without_key(service, claim: dict[str, object]) -> dict[str, object]:
        evidence = local_authority.RoutineEvidence(KEY, record.lease_sha256(claim["lease_token"]), "a" * 32, 0)
        return service.run_routine(
            "team_1",
            claim["run_id"],
            evidence,
            (claim["revision"], claim["plan_digest"], claim["mode"]),
            ("openai", ""),
        )


class ExecutionTests(CompiledRunCase):
    def test_a_healthy_run_makes_no_model_call_and_seals_each_dispatch_before_its_rpc(self) -> None:
        calls: list[tuple[str, dict[str, object], str, object]] = []

        def invoke(_team, _assistant, action, payload, evidence):
            run_id = claim["run_id"]
            sealed = service.routine_store.cursor("team_1", binding(run_id))
            # The recovery snapshot and this dispatch's logical operation are durable before the RPC.
            self.assertIsNotNone(service.routine_store.recovery("team_1", run_id))
            self.assertEqual(sealed.operation_id, evidence.operation_id)
            calls.append((action, dict(payload), evidence.operation_id, sealed.step))
            return {"result": ZONES if action == "list-zones" else RECORDS}

        with tempfile.TemporaryDirectory() as directory:
            controller, service, brain, value = self.compiled(directory, invoke)
            network = controller.assistant_lifecycle._network("team_1").id

            def binding(run_id: str) -> routine_cursor.Binding:
                return routine_cursor.Binding(network, value.routine_id, value.revision, run_id)

            claim = service.claim_routine_run()
            result = self.run_without_key(service, claim)
            state = self.state(service)
            leftovers = (service.routine_store.cursors("team_1"), service.routine_store.recoveries("team_1"))
        self.assertEqual(result["status"], "done")
        self.assertEqual(brain.calls, [])
        self.assertEqual(
            [(action, step) for action, _payload, _operation, step in calls],
            [("list-zones", 0), ("list-dns-records", 1)],
        )
        self.assertEqual(calls[1][1], {**LOOKUP_INPUT, "zone_id": ZONE})
        self.assertNotEqual(calls[0][2], calls[1][2])
        # The plan shows its last step's result: Team's bounded projection of the records it listed, in sorted key
        # order, with no model call (ADR-0092 amendment, 2026-10-05, output).
        pagination = {
            "kind": "fields",
            "fields": [
                ["count", {"kind": "number", "value": "0"}],
                ["page", {"kind": "number", "value": "1"}],
                ["per_page", {"kind": "number", "value": "25"}],
                ["total_count", {"kind": "number", "value": "0"}],
                ["total_pages", {"kind": "number", "value": "0"}],
            ],
            "omitted": 0,
        }
        records = {"kind": "list", "items": [], "omitted": 0}
        shown = {"kind": "fields", "fields": [["pagination", pagination], ["records", records]], "omitted": 0}
        output = {"step": "records", "state": "shown", "value": shown, "truncated": False}
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices],
            [("done", {"actions": record.plan_actions(value.plan), "output": output})],
        )
        self.assertEqual(leftovers, ((), ()))

    def test_a_failed_later_step_keeps_the_completed_prefix_and_holds_the_run(self) -> None:
        actions: list[str] = []

        def invoke(_team, _assistant, action, _payload, _evidence):
            actions.append(action)
            if action == "list-dns-records":
                raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-rpc-failed")
            return {"result": ZONES}

        with tempfile.TemporaryDirectory() as directory:
            _controller, service, brain, _value = self.compiled(directory, invoke)
            claim = service.claim_routine_run()
            self.assertEqual(self.run_without_key(service, claim)["status"], "held")
            opened = routine_incident.open_recovery(service, "team_1", claim["run_id"])
        self.assertEqual((actions, brain.calls), (["list-zones", "list-dns-records"], []))
        # The successful prefix is durable: the cursor stands at the failed step with the value it selected.
        self.assertEqual((opened.cursor.step, opened.cursor.selections()), (1, {("zones", "/zones/0/id"): ZONE}))
        self.assertIsNotNone(opened.cursor.operation_id)

    def test_a_missing_reference_holds_and_nothing_runs_when_the_plan_no_longer_admits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, brain, _value = self.compiled(directory, lambda *_args: {"result": LOOKUP_RESULT})
            claim = service.claim_routine_run()
            self.assertEqual(self.run_without_key(service, claim)["status"], "held")
            self.assertEqual(brain.calls, [])
        invoked: list[object] = []
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, _value = self.compiled(directory, lambda *args: invoked.append(args))
            claim = service.claim_routine_run()
            with mock.patch.object(routine_plan, "admit", side_effect=routine_plan.PlanError("plan-pin-drift")):
                self.assertEqual(self.run_without_key(service, claim)["status"], "failed")
            state = self.state(service)
        self.assertEqual(invoked, [])
        self.assertEqual((state.notices[-1].detail, state.incidents), ({"code": "plan-pin-drift", "actions": []}, ()))

    def test_a_human_request_freezes_mid_plan_and_the_answer_never_reruns_the_prefix(self) -> None:
        calls: list[tuple[str, str]] = []

        def invoke(_team, _assistant, action, _payload, evidence):
            calls.append((action, evidence.operation_id))
            if action == "list-dns-records" and not evidence.transcript.responses:
                raise action_human.HumanRequestSuspensionError(approval())
            return {"result": ZONES if action == "list-zones" else RECORDS}

        with tempfile.TemporaryDirectory() as directory:
            _controller, service, brain, value = self.compiled(directory, invoke)
            claim = service.claim_routine_run()
            self.assertEqual(self.run_without_key(service, claim)["status"], "frozen")
            opened = service.open_routine_challenge("team_1", claim["run_id"], "pt")
            answer = {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True}
            resumed = service.resume_routine_human("team_1", claim["run_id"], answer, "openai", "")
            state = self.state(service)
        self.assertEqual((resumed["status"], brain.calls), ("done", []))
        self.assertEqual(
            [action for action, _operation in calls], ["list-zones", "list-dns-records", "list-dns-records"]
        )
        # The replay is the same logical operation.
        self.assertEqual(calls[1][1], calls[2][1])
        detail = state.notices[-1].detail
        self.assertEqual((detail["actions"], detail["output"]["state"]), (record.plan_actions(value.plan), "shown"))

    def test_a_reopened_run_that_cannot_read_its_cursor_is_held_with_its_completed_prefix(self) -> None:
        """Whether a run already acted comes from durable state, never from a runtime that failed to open it."""
        calls: list[str] = []

        def invoke(_team, _assistant, action, _payload, evidence):
            calls.append(action)
            if action == "list-dns-records" and not evidence.transcript.responses:
                raise action_human.HumanRequestSuspensionError(approval())
            return {"result": ZONES if action == "list-zones" else RECORDS}

        with tempfile.TemporaryDirectory() as directory:
            _controller, service, brain, _value = self.compiled(directory, invoke)
            claim = service.claim_routine_run()
            run_id = claim["run_id"]
            self.assertEqual(self.run_without_key(service, claim)["status"], "frozen")
            opened = service.open_routine_challenge("team_1", run_id, "pt")
            answer = {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True}
            unreadable = routine_store.RoutineStoreError("Routine cursor could not be read")
            with mock.patch.object(service.routine_store, "cursor", side_effect=unreadable):
                resumed = service.resume_routine_human("team_1", run_id, answer, "openai", "")
            state = self.state(service)
            recovered = routine_incident.open_recovery(service, "team_1", run_id)
        self.assertEqual((resumed["status"], brain.calls, calls), ("held", [], ["list-zones", "list-dns-records"]))
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        # The unreadable cursor leaves the held notice without a step; it never blocks the incident.
        self.assertEqual(state.notices[-1].detail, {"assistant_id": None, "action": None})
        # The completed first step and the dispatched second one stay as evidence, never cleaned up as a failure.
        self.assertEqual(recovered.cursor.step, 1)
        self.assertIsNotNone(recovered.cursor.operation_id)


class DiagnosticTests(CompiledRunCase):
    """A failed attempt's sanitized failure or transport condition outlives the segment, its archive, and a restart."""

    def failing(self, directory: str, problem: BaseException):
        def invoke(_team, _assistant, action, _payload, _evidence):
            if action == "list-dns-records":
                raise problem
            return {"result": ZONES}

        _controller, service, _brain, _value = self.compiled(directory, invoke)
        claim = service.claim_routine_run()
        self.assertEqual(self.run_without_key(service, claim)["status"], "held")
        # A restarted Team opens its diagnostics with a fresh store over the same encrypted family.
        service.routine_diagnostics = local_routine_diagnostics.DiagnosticStore(
            service.routine_diagnostics.root, service.routine_diagnostics.key_path
        )
        opened = routine_incident.open_recovery(service, "team_1", claim["run_id"])
        return service, claim["run_id"], opened.cursor.operation_id

    @staticmethod
    def problem(cause: BaseException) -> local_app.ApiProblem:
        try:
            raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-action-failed") from cause
        except local_app.ApiProblem as exc:
            return exc

    def test_a_handled_failure_is_kept_for_its_operation_and_read_after_a_restart(self) -> None:
        failure = action_failure.ActionFailure(
            "httpx.HTTPStatusError", "Not Found", "api.cloudflare.com", 404, None, False, False
        )
        with tempfile.TemporaryDirectory() as directory:
            service, run_id, operation_id = self.failing(
                directory, self.problem(action_failure.ActionFailedError(failure))
            )
            details = service.routine_run_diagnostics("team_1", run_id, int(time.time()))
        (diagnostic,) = details["diagnostics"]
        self.assertEqual(
            (diagnostic["operation_id"], diagnostic["attempt"], diagnostic["action"], diagnostic["condition"]),
            (operation_id, 1, "list-dns-records", None),
        )
        self.assertEqual(diagnostic["failure"], failure.document())

    def test_a_transport_condition_is_kept_and_anything_else_keeps_nothing(self) -> None:
        cases = (
            (action_execution.RpcExchangeError("timeout"), [(None, "timeout")]),
            (action_execution.RpcExchangeError("raw", "Traceback: secret"), []),
            (ValueError("unexplained"), []),
        )
        for cause, expected in cases:
            with tempfile.TemporaryDirectory() as directory, self.subTest(cause=cause):
                service, run_id, _operation = self.failing(directory, self.problem(cause))
                details = service.routine_run_diagnostics("team_1", run_id, int(time.time()))
                self.assertEqual([(item["failure"], item["condition"]) for item in details["diagnostics"]], expected)

    def test_only_a_near_cause_is_ever_read(self) -> None:
        deep: BaseException = action_execution.RpcExchangeError("timeout")
        for _depth in range(8):
            deep = self.problem(deep)
        self.assertIsNone(local_routine_diagnostics.evidence(deep))
        self.assertEqual(local_routine_diagnostics.evidence(deep.__cause__), (None, "timeout"))

    def test_a_diagnostic_that_would_hold_an_injected_value_is_never_kept(self) -> None:
        evidence = action_execution.ActionInvocationEvidence(
            action_execution.RpcPrivateInputs(
                {"cloudflare": {"access_token": "tok-123", "scopes": ["a"], "expires_in": 3600}}, {"key": "k"}
            ),
            action_human.ActionTranscript("i-1", ()),
            "",
            "0" * 32,
        )
        self.assertEqual(set(local_routine_diagnostics.protected(evidence)), {"tok-123", "a", "k"})
        leaked = action_failure.ActionFailure("Error", "token tok-123 refused", None, None, None, False, False)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(local_routine_diagnostics, "protected", return_value=("tok-123",)):
                service, run_id, _operation = self.failing(
                    directory, self.problem(action_failure.ActionFailedError(leaked))
                )
            details = service.routine_run_diagnostics("team_1", run_id, int(time.time()))
        self.assertEqual(details["diagnostics"], [])


class Crash(BaseException):
    """The Team process dies here: nothing after this point of the segment runs, and its run stays leased."""


class WatchdogRecoveryTests(CompiledRunCase):
    """After a crash, the watchdog ends a leased run exactly as its sealed cursor, snapshot, and journal show."""

    def crashed(self, directory: str, patch) -> tuple[object, str, list[str]]:
        actions: list[str] = []

        def invoke(_team, _assistant, action, _payload, _evidence):
            actions.append(action)
            return {"result": ZONES if action == "list-zones" else RECORDS}

        _controller, service, brain, _value = self.compiled(directory, invoke)
        claim = service.claim_routine_run()
        with patch(service), self.assertRaises(Crash):
            self.run_without_key(service, claim)
        self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "leased")
        routine_watchdog.check(service, startup=True)
        self.assertEqual(brain.calls, [])
        return service, claim["run_id"], actions

    def test_a_crash_after_an_actions_receipt_holds_the_run_with_its_dispatch(self) -> None:
        def patch(_service):
            return mock.patch.object(routine_cursor, "complete", side_effect=Crash)

        with tempfile.TemporaryDirectory() as directory:
            service, run_id, actions = self.crashed(directory, patch)
            state = self.state(service)
            opened = routine_incident.open_recovery(service, "team_1", run_id)
            archived = service.action_state.current_batch(state.incidents[0].generation)
        self.assertEqual((actions, state.runs), (["list-zones"], ()))
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        self.assertEqual((opened.cursor.step, archived[1]), (0, "archived"))
        self.assertIsNotNone(opened.cursor.operation_id)

    def test_a_crash_after_the_cursor_advanced_holds_the_run_with_its_completed_prefix(self) -> None:
        def patch(service):
            put = service.routine_store.put_cursor

            def sealed_then_crash(team_id, cursor):
                put(team_id, cursor)
                if cursor.step == 1:
                    raise Crash

            return mock.patch.object(service.routine_store, "put_cursor", side_effect=sealed_then_crash)

        with tempfile.TemporaryDirectory() as directory:
            service, run_id, actions = self.crashed(directory, patch)
            state = self.state(service)
            opened = routine_incident.open_recovery(service, "team_1", run_id)
        self.assertEqual((actions, state.runs), (["list-zones"], ()))
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        self.assertEqual((opened.cursor.step, opened.cursor.operation_id), (1, None))
        self.assertEqual(opened.cursor.selections(), {("zones", "/zones/0/id"): ZONE})

    def test_a_crash_before_the_terminal_commit_finishes_a_completed_run_done(self) -> None:
        def patch(_service):
            return mock.patch.object(routine_run, "finished", side_effect=Crash)

        with tempfile.TemporaryDirectory() as directory:
            service, _run_id, actions = self.crashed(directory, patch)
            state = self.state(service)
            leftovers = (service.routine_store.cursors("team_1"), service.routine_store.recoveries("team_1"))
        self.assertEqual((actions, state.runs, state.incidents), (["list-zones", "list-dns-records"], (), ()))
        self.assertEqual([item.outcome for item in state.notices], ["done"])
        self.assertEqual(leftovers, ((), ()))

    def test_a_completed_cursor_of_another_plan_is_held_never_finished_done(self) -> None:
        def patch(service):
            put = service.routine_store.put_cursor

            def finished_for_another_plan(team_id, cursor):
                put(team_id, cursor)
                if cursor.step == 2:
                    put(team_id, dataclasses.replace(cursor, plan="sha256:" + "0" * 64))
                    raise Crash

            return mock.patch.object(service.routine_store, "put_cursor", side_effect=finished_for_another_plan)

        with tempfile.TemporaryDirectory() as directory:
            service, run_id, actions = self.crashed(directory, patch)
            state = self.state(service)
        self.assertEqual((actions, state.runs), (["list-zones", "list-dns-records"], ()))
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        self.assertNotIn("done", [item.outcome for item in state.notices])

    def test_a_crash_before_any_dispatch_fails_the_run_interrupted(self) -> None:
        def patch(_service):
            return mock.patch.object(routine_compiled.CompiledRuntime, "dispatching", side_effect=Crash)

        with tempfile.TemporaryDirectory() as directory:
            service, _run_id, actions = self.crashed(directory, patch)
            state = self.state(service)
        self.assertEqual((actions, state.incidents), ([], ()))
        self.assertEqual([item.detail for item in state.notices], [{"code": "interrupted", "actions": []}])


class AssistantProcess:
    """The Assistant's side of Docker's exec attach: it reads one invocation frame and answers one result frame."""

    def __init__(self) -> None:
        self.invocations: list[dict[str, object]] = []

    def exec_create(self, _container, argv, **_kwargs):
        return {"Id": argv[-1]}

    def exec_start(self, exec_id, socket: bool):
        team_side, assistant_side = socketpair()

        def serve() -> None:
            raw = b""
            while chunk := assistant_side.recv(65536):
                raw += chunk
            invocation = json.loads(raw)
            self.invocations.append({"action": exec_id, **invocation})
            result = ZONES if exec_id == "list-zones" else RECORDS
            payload = json.dumps({"type": "result", "result": result}).encode()
            assistant_side.sendall(struct.pack(">BxxxL", 1, len(payload)) + payload)
            assistant_side.close()

        threading.Thread(target=serve, daemon=True).start()
        return SimpleNamespace(_sock=team_side, close=team_side.close)

    @staticmethod
    def exec_inspect(_exec_id):
        return {"ExitCode": 0}


class RealRpcTests(CompiledRunCase):
    def test_a_run_reaches_the_assistant_over_its_real_rpc_with_no_brain_and_no_key(self) -> None:
        process = AssistantProcess()
        with tempfile.TemporaryDirectory() as directory:
            controller, service, brain, value = self.compiled(directory, None)
            controller.assistant_lifecycle.invoke = controller.invoke
            controller.assistant_lifecycle.client = SimpleNamespace(api=process)
            claim = service.claim_routine_run()
            result = self.run_without_key(service, claim)
            state = self.state(service)
        self.assertEqual((result["status"], brain.calls), ("done", []))
        first, second = process.invocations
        self.assertEqual((first["action"], first["input"]), ("list-zones", LOOKUP_INPUT))
        self.assertEqual((second["action"], second["input"]), ("list-dns-records", {**LOOKUP_INPUT, "zone_id": ZONE}))
        # Each step is its own logical operation, and the Integration token reached only the Assistant.
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(set(first["integrations"]), {"cloudflare"})
        detail = state.notices[-1].detail
        self.assertEqual((detail["actions"], detail["output"]["state"]), (record.plan_actions(value.plan), "shown"))


class StopTests(CompiledRunCase):
    def test_a_stop_during_an_action_holds_the_run_and_a_stop_between_runs_stops_it(self) -> None:
        def invoke(*_args):
            # Stop reaches the running segment while its first Action is in flight.
            service.stop_routine("team_1", claim["run_id"])
            return {"result": ZONES}

        with tempfile.TemporaryDirectory() as directory:
            controller, service, brain, _value = self.compiled(directory, invoke)
            claim = service.claim_routine_run()
            with mock.patch.object(controller.assistant_lifecycle, "_fail_stop_action"):
                outcome = self.run_without_key(service, claim)["status"]
            state = self.state(service)
        # The stopped Action's effect is unknown, so the run is held for recovery instead of ending stopped.
        self.assertEqual(
            (outcome, [item.incident_id for item in state.incidents], brain.calls), ("held", [claim["run_id"]], [])
        )

    def test_a_held_run_is_never_stopped_out_of_its_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            held = record.Run("d" * 32, value.routine_id, "held", 0, generation=f"{'a' * 64}:routine:{'d' * 32}")
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=(held,)), None))
            self.assertFalse(service.stop_routine("team_1", "d" * 32)["stopped"])
            self.assertEqual(self.state(service).runs, (held,))


class RuntimeTests(CompiledRunCase):
    def runtime(self, service, value: record.Routine) -> routine_compiled.CompiledRuntime:
        claim = service.claim_routine_run()
        network = "a" * 64
        run = record.Run(
            claim["run_id"], value.routine_id, "leased", 0, generation=f"{network}:routine:{claim['run_id']}"
        )
        return routine_compiled.runtime(service, "team_1", run, value)

    def test_the_runtime_refuses_a_foreign_step_a_lost_cursor_and_an_unresolvable_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            compiled = self.runtime(service, value)
            self.assertIsNone(compiled.purpose(None, None, "", ""))
            foreign = brain_runtime_client.ActionRequest("routine-step-1", "x", "y", {})
            with self.assertRaises(routine_compiled.CompiledRunError) as caught:
                compiled.dispatching(foreign, "0" * 32)
            self.assertEqual(caught.exception.code, "routine-step-changed")
            # A failure is classified only for a dispatched operation.
            with self.assertRaises(routine_compiled.CompiledRunError) as undispatched:
                compiled.failed(foreign, None, RuntimeError("x"))
            self.assertEqual(undispatched.exception.code, "cursor-not-dispatched")
            first = compiled.start(None, "")
            with self.assertRaises(routine_compiled.CompiledRunError) as invalid:
                compiled.dispatching(first.actions[0], "not-an-operation")
            self.assertEqual(invalid.exception.code, "cursor-dispatch-invalid")
            with self.assertRaises(routine_compiled.CompiledRunError) as unselected:
                compiled.resume(None, {"routine-step-0": {"zones": []}})
            self.assertEqual(unselected.exception.code, "cursor-not-dispatched")
            # A cursor past the first step without the value the next step selects cannot resolve its input.
            skipped = routine_cursor.Cursor(
                compiled.cursor.binding, compiled.cursor.plan, compiled.cursor.started_at, 1
            )
            compiled.cursor = skipped
            with self.assertRaises(routine_compiled.CompiledRunError) as missing:
                compiled.start(None, "")
            self.assertEqual(missing.exception.code, "plan-reference-missing")
            with (
                mock.patch.object(
                    service.routine_store, "put_cursor", side_effect=routine_store.RoutineStoreError("x")
                ),
                self.assertRaises(routine_compiled.CompiledRunError) as lost,
            ):
                compiled.seal(skipped)
            self.assertEqual(lost.exception.code, "routine-cursor-unavailable")

    def test_a_cursor_that_cannot_be_read_or_names_another_plan_never_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            compiled = self.runtime(service, value)
            run = record.Run(
                compiled.cursor.binding.run_id,
                value.routine_id,
                "leased",
                0,
                generation=f"{'a' * 64}:routine:{compiled.cursor.binding.run_id}",
            )
            with (
                mock.patch.object(service.routine_store, "cursor", side_effect=routine_store.RoutineStoreError("x")),
                self.assertRaises(routine_compiled.CompiledRunError) as unreadable,
            ):
                routine_compiled.runtime(service, "team_1", run, value)
            self.assertEqual(unreadable.exception.code, "routine-cursor-unavailable")
            other = SimpleNamespace(plan="sha256:" + "0" * 64)
            with (
                mock.patch.object(service.routine_store, "cursor", return_value=other),
                self.assertRaises(routine_compiled.CompiledRunError) as changed,
            ):
                routine_compiled.runtime(service, "team_1", run, value)
            self.assertEqual(changed.exception.code, "cursor-plan-changed")

    def test_progress_is_read_from_the_sealed_snapshot_cursor_and_journal_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            compiled = self.runtime(service, value)
            run_id = compiled.cursor.binding.run_id
            run = record.Run(
                run_id,
                value.routine_id,
                "leased",
                0,
                generation=f"{compiled.cursor.binding.incarnation}:routine:{run_id}",
            )
            store, journal = service.routine_store, service.action_state
            batch = ("f" * 64, "open")
            dispatched = dataclasses.replace(compiled.cursor, operation_id="0" * 32)
            completed = dataclasses.replace(compiled.cursor, step=2)
            cases = (
                ({}, run, "none"),
                ({}, dataclasses.replace(run, generation=""), "none"),
                ({}, dataclasses.replace(run, routine_id="0" * 32), "partial"),
                ({"current_batch": batch}, run, "none"),
                ({"cursor": None, "current_batch": batch}, run, "partial"),
                ({"recovery": None}, run, "none"),
                ({"recovery": None, "current_batch": batch}, run, "partial"),
                ({"cursor": None}, run, "none"),
                ({"cursor": dispatched}, run, "partial"),
                ({"cursor": completed}, run, "done"),
                ({"cursor": dataclasses.replace(completed, step=1)}, run, "partial"),
                # A clean or complete cursor of another plan holds the run.
                ({"cursor": dataclasses.replace(completed, plan="sha256:" + "0" * 64)}, run, "partial"),
                ({"cursor": dataclasses.replace(compiled.cursor, plan="sha256:" + "0" * 64)}, run, "partial"),
            )
            for patched, subject, expected in cases:
                with contextlib.ExitStack() as stack, self.subTest(patched=patched, expected=expected):
                    for name, result in patched.items():
                        owner = journal if name == "current_batch" else store
                        stack.enter_context(mock.patch.object(owner, name, return_value=result))
                    self.assertEqual(routine_compiled.progress(service, "team_1", subject), expected)
            failing = action_journal.ActionJournalError("journal unavailable")
            with mock.patch.object(journal, "current_batch", side_effect=failing):
                self.assertEqual(routine_compiled.progress(service, "team_1", run), "partial")
            with mock.patch.object(journal, "uncertain_fingerprint", side_effect=failing):
                self.assertTrue(routine_compiled._uncertain(service, run, []))


class ShownResultTests(CompiledRunCase):
    """The plan's shown step keeps its bounded result and keyed digest in the sealed cursor (ADR-0092, 2026-10-05)."""

    OPERATION = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"

    runtime = RuntimeTests.runtime

    def shown_runtime(self, service, value: record.Routine) -> routine_compiled.CompiledRuntime:
        compiled = self.runtime(service, value)
        compiled.seal(dataclasses.replace(compiled.cursor, step=1, selected=(("zones", "/zones/0/id", ZONE),)))
        request = compiled.start(None, "").actions[0]
        compiled.dispatching(request, self.OPERATION)
        return compiled

    def test_the_shown_step_keeps_its_result_and_a_keyed_digest_bound_to_its_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            compiled = self.shown_runtime(service, value)
            turn = compiled.resume(None, {"routine-step-1": RECORDS})
            shown = compiled.cursor.shown
            store, binding = service.routine_store, compiled.cursor.binding
            material = routine_plan.canonical(routine_plan.output_safe(RECORDS, {}))
            again = store.output_digest("team_1", binding, "records", material)
            others = {
                store.output_digest("team_1", dataclasses.replace(binding, revision=2), "records", material),
                store.output_digest("team_1", binding, "zones", material),
                store.output_digest("team_1", binding, "records", material + b" "),
                store.output_digest("team_2", binding, "records", material),
            }
            sealed = routine_compiled.sealed_shown(
                service, "team_1", record.Run(binding.run_id, value.routine_id, "leased", 0, generation="x")
            )
        self.assertEqual(turn.status, "completed")
        self.assertEqual((shown["step"], shown["output"]["state"], shown["digest"]), ("records", "shown", again))
        # The digest is keyed: it is neither the material's plain hash nor equal for any other binding or result.
        self.assertNotEqual(again, hashlib.sha256(material).hexdigest())
        self.assertNotIn(again, others)
        self.assertEqual(len(others), 4)
        # A run whose sealed state cannot be read keeps no shown result.
        self.assertIsNone(sealed)

    def test_a_result_that_cannot_be_projected_or_digested_never_passes_as_shown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            compiled = self.shown_runtime(service, value)
            dispatched = compiled.cursor
            with mock.patch.object(routine_plan, "output_safe", side_effect=routine_plan.OutputError("x")):
                compiled.resume(None, {"routine-step-1": RECORDS})
            unavailable = compiled.cursor.shown
            compiled.cursor = dispatched
            with (
                mock.patch.object(
                    service.routine_store, "output_digest", side_effect=routine_store.RoutineStoreError("x")
                ),
                self.assertRaises(routine_compiled.CompiledRunError) as lost,
            ):
                compiled.resume(None, {"routine-step-1": RECORDS})
        self.assertEqual(
            unavailable,
            {"step": "records", "output": routine_plan.output_state("records", "unavailable"), "digest": None},
        )
        self.assertEqual(lost.exception.code, "routine-cursor-unavailable")

    def test_a_result_with_a_number_too_long_for_a_float_completes_and_shows_it_exactly(self) -> None:
        huge = {"records": [], "pagination": {**RECORDS["pagination"], "count": 10**400}}
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            compiled = self.shown_runtime(service, value)
            turn = compiled.resume(None, {"routine-step-1": huge})
            shown = compiled.cursor.shown
        self.assertEqual((turn.status, shown["output"]["state"]), ("completed", "shown"))
        pagination = dict(shown["output"]["value"]["fields"])["pagination"]
        self.assertEqual(dict(pagination["fields"])["count"]["value"], str(10**400)[:299] + "…")
        self.assertIsNotNone(shown["digest"])
