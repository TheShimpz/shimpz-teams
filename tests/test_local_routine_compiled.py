"""A compiled Routine run executes its plan with no model and no model key (ADR-0092 sections 3, 5, and 6)."""

from __future__ import annotations

import dataclasses
import tempfile
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from test_local_chat_scope import LOOKUP_INPUT, LOOKUP_RESULT
from test_local_routine_service import KEY, RoutineServiceCase, approval

from action import human as action_human
from inference import client as brain_runtime_client
from local import app as local_app
from local import authority as local_authority
from local.routine import compiled as routine_compiled
from local.routine import incident as routine_incident
from local.routine import store as routine_store
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
        return service.run_routine("team_1", claim["run_id"], evidence, "openai", "")


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

            claim = service.claim_routine_run(("anthropic", "openai"))
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
        self.assertEqual([(item.outcome, item.detail) for item in state.notices], [("done", {"reply": value.name})])
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
            claim = service.claim_routine_run(("anthropic", "openai"))
            self.assertEqual(self.run_without_key(service, claim)["status"], "held")
            opened = routine_incident.open_recovery(service, "team_1", claim["run_id"])
        self.assertEqual((actions, brain.calls), (["list-zones", "list-dns-records"], []))
        # The successful prefix is durable: the cursor stands at the failed step with the value it selected.
        self.assertEqual((opened.cursor.step, opened.cursor.selections()), (1, {("zones", "/zones/0/id"): ZONE}))
        self.assertIsNotNone(opened.cursor.operation_id)

    def test_a_missing_reference_holds_and_nothing_runs_when_the_plan_no_longer_admits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, brain, _value = self.compiled(directory, lambda *_args: {"result": LOOKUP_RESULT})
            claim = service.claim_routine_run(("anthropic", "openai"))
            self.assertEqual(self.run_without_key(service, claim)["status"], "held")
            self.assertEqual(brain.calls, [])
        invoked: list[object] = []
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, _value = self.compiled(directory, lambda *args: invoked.append(args))
            claim = service.claim_routine_run(("anthropic", "openai"))
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
            claim = service.claim_routine_run(("anthropic", "openai"))
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
        self.assertEqual(state.notices[-1].detail, {"reply": value.name})


class StopTests(CompiledRunCase):
    def test_a_stop_during_an_action_holds_the_run_and_a_stop_between_runs_stops_it(self) -> None:
        def invoke(*_args):
            # Stop reaches the running segment while its first Action is in flight.
            service.stop_routine("team_1", claim["run_id"])
            return {"result": ZONES}

        with tempfile.TemporaryDirectory() as directory:
            controller, service, brain, _value = self.compiled(directory, invoke)
            claim = service.claim_routine_run(("anthropic", "openai"))
            with mock.patch.object(controller.assistant_lifecycle, "_fail_stop_action"):
                outcome = self.run_without_key(service, claim)["status"]
            state = self.state(service)
        # The stopped Action's effect is unknown, so the run is held for recovery instead of ending stopped.
        self.assertEqual(
            (outcome, [item.incident_id for item in state.incidents], brain.calls), ("held", [claim["run_id"]], [])
        )

    def test_an_uncertain_run_needs_a_resolution_not_a_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _brain, value = self.compiled(directory, None)
            uncertain = record.Run(
                "d" * 32,
                value.routine_id,
                "uncertain",
                0,
                generation=f"{'a' * 64}:routine:{'d' * 32}",
                batch=(f"{'a' * 64}:routine:{'d' * 32}", "e" * 64),
            )
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=(uncertain,)), None))
            with self.assertRaises(local_app.ApiProblem) as caught:
                service.stop_routine("team_1", "d" * 32)
            self.assertEqual(caught.exception.code, "routine-run-uncertain")


class RuntimeTests(CompiledRunCase):
    def runtime(self, service, value: record.Routine) -> routine_compiled.CompiledRuntime:
        claim = service.claim_routine_run(("anthropic", "openai"))
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
