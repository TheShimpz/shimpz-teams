"""A Routine of 120 steps, one Action used 118 times with its own inputs, from the person's message to its shown result.

Its creation, sealing, claim, execution through the real turn loop, notices, plan pages, and run step records all stay
within the bounds the admission budget derives (ADR-0092 amendment, 2026-10-05, scale).
"""

from __future__ import annotations

import dataclasses
import tempfile

from test_local_chat_scope import LOOKUP_RESULT
from test_local_routine_create import Runtime as CreateRuntime
from test_local_routine_create import _body, _change, _origin
from test_local_routine_service import KEY, RoutineServiceCase

from local import audit as local_audit
from local.routine import store as routine_store
from protocol.http.v1 import routine as http_routine
from routine import grant as routine_grant
from routine import plan as routine_plan
from routine import record

ASSISTANT = "shimpz-cloudflare"
ZONE = "c" * 32
PAGES = range(1, 119)
MESSAGE = (
    "Every Monday at 9:00, list my zones, pages "
    + " ".join(str(page) for page in PAGES)
    + " with 5 per page, then list the records of the first zone and show me the records"
)
RELATION = "the records of the first zone"


def _scaled_change() -> dict[str, object]:
    """A compiled change of 120 steps: list-zones once per page, then the first zone's records twice."""
    steps = [
        {
            "id": f"zones{page}",
            "assistant": ASSISTANT,
            "action": "list-zones",
            "input": {
                "page": {"kind": "literal", "value": page, "origins": [_origin(str(page))]},
                "per_page": {"kind": "literal", "value": 5, "origins": [_origin("5")]},
            },
        }
        for page in PAGES
    ]
    for step_id, kind in (("records", "step_output"), ("again", "step_text")):
        first = {"kind": kind, "step": "zones1", "pointer": "/zones/0/id", "instruction": RELATION}
        steps.append(
            {
                "id": step_id,
                "assistant": ASSISTANT,
                "action": "list-dns-records",
                "input": {
                    "zone_id": first,
                    "page": {"kind": "literal", "value": 1, "origins": [_origin("1")]},
                    "per_page": {"kind": "literal", "value": 5, "origins": [_origin("5")]},
                },
            }
        )
    output = {"mode": "show", "step": "again", "instruction": "show me the records"}
    return _change(steps=steps, output=output, request="Every Monday at 9:00, list my zones")


class ScaleJourneyTests(RoutineServiceCase):
    def test_a_120_step_routine_is_created_sealed_run_and_shown_within_its_bounds(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(_team, _assistant, action, payload, _evidence):
            calls.append((action, dict(payload)))
            if action == "list-zones":
                return {"result": {**LOOKUP_RESULT, "zones": [{"id": ZONE, "name": "example.com"}]}}
            return {"result": {"records": [], "pagination": LOOKUP_RESULT["pagination"]}}

        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, CreateRuntime(_scaled_change()))
            controller.assistant_lifecycle.invoke = invoke
            with local_audit.bind_request_principal(local_audit.AuditPrincipal("a" * 32, "human")):
                service.chat("team_1", _body(MESSAGE), "openai", "sk-test-0123456789")
            (value,) = self.state(service).routines
            # Created, its words sealed for Recriar, within its definition budget.
            self.assertIsNotNone(service.routine_store.source("team_1", value.routine_id))
            self.assertLessEqual(routine_grant.definition_bytes(value), routine_plan.MAX_DEFINITION_BYTES)
            (created,) = self.state(service).notices
            summary = created.detail["plan"]
            self.assertEqual(
                (summary["steps"], summary["actions"]),
                (120, [[ASSISTANT, "list-zones", 118], [ASSISTANT, "list-dns-records", 2]]),
            )
            self.assertEqual(created.detail["output"], {"mode": "show", "step": 120})
            # The Brain's listing projects the plan: no step keeps its pin or any input's source, so a listing never
            # outgrows the plan bytes Team admitted, which the Brain's listing bound relies on.
            (listed,) = service._chat_routines("team_1")
            self.assertEqual((len(listed["steps"]), listed["daily_steps"]), (120, 120))
            self.assertLess(len(routine_plan.canonical(listed["steps"])), len(routine_plan.canonical(value.plan)))
            # Its plan reads page by page, every step once and in order.
            positions, offset = [], 0
            while offset is not None:
                page = service.routine_steps("team_1", value.routine_id, value.revision, offset)
                self.assertEqual(http_routine.canonical_page(page), page)
                positions.extend(step["position"] for step in page["steps"])
                offset = page["next"]
            self.assertEqual(positions, list(range(1, 121)))
            # A long run is never claimed while Admin already holds one; its active time grows with its steps.
            self._due(service, value)
            self.assertIsNone(service.claim_routine_run(False))
            claim = service.claim_routine_run(True)
            self.assertEqual(claim["active_seconds"], routine_plan.active_seconds(120))
            result = self.run_claim(service, claim)
            state = self.state(service)
            steps_view = self._run_steps(service, claim["run_id"])
        self.assertEqual(result["status"], "done")
        # One Action 118 times with its own inputs, then the first zone's records by value and as text.
        self.assertEqual(
            [payload for _action, payload in calls[:118]], [{"page": page, "per_page": 5} for page in PAGES]
        )
        self.assertEqual(calls[118:], [("list-dns-records", {"zone_id": ZONE, "page": 1, "per_page": 5})] * 2)
        done = state.notices[-1]
        self.assertEqual(
            (done.outcome, done.detail["plan"]["steps"], done.detail["output"]["step"]), ("done", 120, 120)
        )
        self.assertLessEqual(len(routine_store._encode(state, "team_1")), routine_store.MAX_STATE_BYTES)
        # The run's own records say what each step did with the inputs it was given, bound to its revision.
        self.assertEqual([step["status"] for step in steps_view], ["done"] * 120)
        self.assertEqual(
            steps_view[41]["inputs"],
            [
                {"member": "page", "source": "literal", "value": "42"},
                {"member": "per_page", "source": "literal", "value": "5"},
            ],
        )
        self.assertEqual(
            steps_view[119]["inputs"][2], {"member": "zone_id", "source": "step_text", "value": f'"{ZONE}"'}
        )

    def _due(self, service, value: record.Routine) -> None:
        import dataclasses
        import time

        service.routine_store.update(
            "team_1",
            lambda state: (
                record._replace_routine(
                    state,
                    dataclasses.replace(record.routine(state, value.routine_id), next_run_at=int(time.time()) - 1),
                ),
                None,
            ),
        )

    @staticmethod
    def _run_steps(service, run_id: str) -> list[dict[str, object]]:
        steps, offset, snapshot = [], 0, "latest"
        while offset is not None:
            page = service.routine_run_steps("team_1", run_id, snapshot, offset, int(__import__("time").time()))
            snapshot = page["snapshot"]
            steps.extend(page["steps"])
            offset = page["next"]
            ended = page["ended"]
        return steps if ended else []


class StepRecordEdgeTests(RoutineServiceCase):
    """A step or run record is a display record: what it cannot keep is audited and never changes the run."""

    def runtime(self, service, value: record.Routine, diagnostics_store) -> object:
        from local.routine import compiled as routine_compiled
        from routine import cursor as routine_cursor

        network = service.assistant_lifecycle._network("team_1").id
        plan = routine_compiled._plan(service, "team_1", value)
        binding = routine_cursor.Binding(network, value.routine_id, value.revision, "e" * 32)
        seal = routine_compiled._Seal("team_1", service.routine_store, diagnostics_store)
        return routine_compiled.CompiledRuntime(seal, plan, routine_cursor.start(plan, binding, 0), "Done.")

    def test_a_record_too_large_keeps_no_inputs_and_one_that_cannot_be_kept_is_audited(self) -> None:
        from unittest import mock

        from local.routine import compiled as routine_compiled
        from local.routine import diagnostics as routine_diagnostics

        kept: list[object] = []
        failing = mock.Mock(side_effect=routine_diagnostics.DiagnosticStoreError("down"))
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, CreateRuntime())
            value = self.routine(service)
            runtime = self.runtime(service, value, mock.Mock(record_step=lambda *args: kept.append(args)))
            runtime.record_step("done")
            self.assertEqual(kept, [])
            runtime._attempt = routine_compiled._Attempt(1, 0.0, {"page": 1, "per_page": 5}, ())
            with mock.patch.object(routine_compiled.http_routine, "MAX_STEP_VIEW_BYTES", 64):
                runtime.record_step("done")
            self.assertIsNone(kept[0][2].inputs)
            broken = self.runtime(service, value, mock.Mock(record_step=failing))
            broken._attempt = routine_compiled._Attempt(1, 0.0, {"page": 1, "per_page": 5}, ())
            with mock.patch.object(routine_compiled.local_audit, "record_request") as audited:
                broken.record_step("failed")
            self.assertEqual(audited.call_args.args[0], "routine-step-record")
            # A recovered step whose input can no longer be resolved withholds every preview, and has no duration.
            recovered: list[object] = []
            service.routine_diagnostics = mock.Mock(record_step=lambda *args: recovered.append(args[2]))
            cursor = runtime.cursor
            with mock.patch.object(
                routine_plan, "resolve", side_effect=routine_plan.PlanError("plan-reference-missing")
            ):
                routine_compiled.record_recovered(service, "team_1", cursor, runtime.plan, ())
            self.assertEqual((recovered[0].status, recovered[0].duration_ms), ("recovered", None))
            self.assertEqual({item["value"] for item in recovered[0].inputs}, {None})

    def test_a_terminal_record_is_kept_only_from_a_readable_snapshot_and_cursor(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from local.routine import diagnostics as routine_diagnostics
        from local.routine import incident as routine_incident
        from local.routine import manage as routine_manage
        from routine import cursor as routine_cursor

        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, CreateRuntime())
            value = self.routine(service)
            runtime = self.runtime(service, value, service.routine_diagnostics)
            service.routine_store.put_cursor("team_1", runtime.cursor)
            snapshot = routine_incident.Recovery(runtime.cursor.binding, value.quote, value.plan)
            recorded: list[object] = []
            service.routine_diagnostics = mock.Mock(record_run=lambda *args: recorded.append(args[2]))
            other = dataclasses.replace(snapshot, binding=dataclasses.replace(snapshot.binding, run_id="f" * 32))
            for refused in (None, other):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, refused)
            with mock.patch.object(service.routine_store, "cursor", side_effect=routine_store.RoutineStoreError("x")):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            with mock.patch.object(service.routine_store, "cursor", return_value=None):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            moved = dataclasses.replace(runtime.cursor, plan="sha256:" + "0" * 64)
            with mock.patch.object(service.routine_store, "cursor", return_value=moved):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            self.assertEqual(recorded, [])
            routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            self.assertEqual((recorded[0].reached, recorded[0].dispatched, recorded[0].binding.total), (0, False, 1))
            service.routine_diagnostics = mock.Mock(
                record_run=mock.Mock(side_effect=routine_diagnostics.DiagnosticStoreError("down"))
            )
            with mock.patch.object(routine_incident.local_audit, "record_request") as audited:
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            self.assertEqual(audited.call_args.args[0], "routine-run-record")
            # A recovery snapshot that cannot be read proves nothing, so none is kept.
            with mock.patch.object(service.routine_store, "recovery", return_value=b"not json"):
                self.assertIsNone(routine_manage._snapshot(service, "team_1", "e" * 32))
            self.assertIsNone(
                routine_manage._snapshot(SimpleNamespace(routine_store=service.routine_store), "team_1", "e" * 32)
            )
            self.assertIsInstance(runtime.cursor, routine_cursor.Cursor)

    def test_an_attempt_lasts_until_its_action_returns_and_a_cut_one_is_stopped(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from chat import orchestrator as chat_orchestrator
        from local.chat import segment as chat_segment
        from local.errors import ApiProblemError
        from local.routine import compiled as routine_compiled
        from local.routine import run as routine_run

        kept: list[object] = []
        operation = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, CreateRuntime())
            value = self.routine(service)
            runtime = self.runtime(service, value, mock.Mock(record_step=lambda *args: kept.append(args[2])))
            # Nothing was dispatched yet: a failure names no step.
            self.assertEqual(routine_compiled._at(SimpleNamespace(runtime=runtime)), {"step": None, "steps": None})
            request = runtime.start(None, "").actions[0]
            # The Action returned 500 ms after it started; sealing and projecting it later never counts.
            with mock.patch.object(routine_compiled, "_STEP_CLOCK", side_effect=[10.0, 10.5]):
                runtime.dispatching(request, operation)
                runtime.returned()
                runtime.returned()
                runtime.record_step("done")
            self.assertEqual((kept[-1].status, kept[-1].duration_ms), ("done", 500))
            self.assertEqual(routine_compiled._at(SimpleNamespace(runtime=runtime)), {"step": 1, "steps": 1})
            # Stop cutting the Action's call records it as stopped, never as failed.
            runtime.dispatching(request, operation)
            with mock.patch.object(routine_compiled.routine_diagnostics, "protected", return_value=()):
                runtime.failed(
                    request, mock.Mock(operation_id=operation), ApiProblemError(409, "x", code="chat-stopped")
                )
            self.assertEqual(kept[-1].status, "stopped")
            # A segment cut while an attempt is still in flight records that attempt as stopped too.
            claim = service.claim_routine_run()
            lease = record.lease_of(claim["lease_token"], KEY)
            run = routine_run._Run("team_1", claim["run_id"], lease, "token", "openai", value)
            # Stop raises out of the Action's call itself; its time still ends there, before the segment unwinds.
            runtime = self.runtime(service, value, mock.Mock(record_step=lambda *args: kept.append(args[2])))
            request = runtime.start(None, "").actions[0]

            def cut() -> object:
                raise chat_orchestrator.ChatStoppedError("chat turn stopped")

            with mock.patch.object(routine_compiled, "_STEP_CLOCK", side_effect=[20.0, 20.5]):
                runtime.dispatching(request, operation)
                with self.assertRaises(chat_orchestrator.ChatStoppedError):
                    chat_segment._observed(cut, runtime, request, None)
            stopped = ApiProblemError(409, "stopped", code="chat-stopped")
            segment = SimpleNamespace(runtime=runtime, batches=[])
            routine_compiled._ended(service, run, record.run(self.state(service), claim["run_id"]), segment, stopped)
            self.assertEqual((kept[-1].status, kept[-1].duration_ms), ("stopped", 500))
            self.assertIsNone(runtime._attempt)

    def test_a_run_frozen_far_into_its_plan_fails_at_that_step(self) -> None:
        from local.routine import human as routine_human

        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, CreateRuntime(_scaled_change()))
            with local_audit.bind_request_principal(local_audit.AuditPrincipal("a" * 32, "human")):
                service.chat("team_1", _body(MESSAGE), "openai", "sk-test-0123456789")
            (value,) = self.state(service).routines
            frozen = record.Run(
                record.new_id(),
                value.routine_id,
                "frozen",
                0,
                request_kind="human",
                assistant_id=ASSISTANT,
                action="list-zones",
                step=37,
                steps=120,
            )
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=(frozen,)), None))
            routine_human._end_changed(service, "team_1", frozen)
            detail = self.state(service).notices[-1].detail
        self.assertEqual((detail["code"], detail["step"], detail["steps"]), ("team-context-changed", 37, 120))

    def test_a_deleting_routine_shows_no_steps(self) -> None:
        from local.errors import ApiProblemError

        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, CreateRuntime())
            value = self.routine(service)
            service.routine_store.update(
                "team_1", lambda state: (record.begin_delete(state, value.routine_id)[0], None)
            )
            with self.assertRaises(ApiProblemError) as caught:
                service.routine_steps("team_1", value.routine_id, value.revision, 0)
        self.assertEqual(caught.exception.code, "routine-not-found")
