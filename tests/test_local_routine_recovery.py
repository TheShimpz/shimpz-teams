"""A held Routine run is verified with no model and continues only on Team-admitted evidence (ADR-0092)."""

from __future__ import annotations

import dataclasses
import tempfile
from http import HTTPStatus

from local_assistant_fixture import mutating_spec
from test_local_chat_scope import LOOKUP_INPUT, LOOKUP_RESULT
from test_local_routine_compiled import ZONE, ZONES, Brain, CompiledRunCase
from test_local_routine_service import ASSISTANT

from action import failure as action_failure
from local import app as local_app
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from routine import cursor as routine_cursor

RECORD = {"record": {"id": "rec-1"}}
NOT_FOUND = action_failure.ActionFailure("HTTPStatusError", "Not Found", "api.cloudflare.com", 404, None, False, False)


def failed() -> local_app.ApiProblem:
    """A handled failure of the provider: a 404 alone never proves the operation had no effect."""
    try:
        raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-action-failed") from (
            action_failure.ActionFailedError(NOT_FOUND)
        )
    except local_app.ApiProblem as exc:
        return exc


class Assistant:
    """The Assistant's side: each create outcome in turn, and each verifier answer in turn."""

    def __init__(self, creates: list[object], verdicts: list[dict[str, object]]) -> None:
        self.creates = creates
        self.verdicts = verdicts
        self.calls: list[tuple[str, str]] = []

    def __call__(self, _team, _assistant, action, payload, evidence):
        self.calls.append((action, evidence.operation_id))
        if action == "list-zones":
            return {"result": ZONES}
        if action == "find-record":
            # The verifier is bound to exactly the held operation's logical id.
            self.calls[-1] = (action, payload["operation_id"])
            return {"result": self.verdicts.pop(0)}
        outcome = self.creates.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"result": outcome}


class RecoveryCase(CompiledRunCase):
    def held(self, directory: str, assistant: Assistant, brain=None, *, automatic: bool = False):
        brain = brain or Brain()
        controller, service = self.service(directory, brain)
        if not automatic:
            # These tests drive verification by hand; the automatic episode has its own tests.
            service._recover_routine_run = lambda _run, _key, _progress=None: "held"
        current = controller.registry[ASSISTANT]
        controller.registry[ASSISTANT] = dataclasses.replace(
            mutating_spec(current.image), provenance=current.provenance, platform=current.platform
        )
        controller.assistant_lifecycle.invoke = assistant
        plan = self.plan(
            service,
            ("zones", "list-zones", LOOKUP_INPUT),
            ("create", "create-record", {"zone_id": ZONE, "name": "www"}),
        )
        value = self.routine(service, plan=plan)
        claim = service.claim_routine_run()
        self.assertEqual(self.run_without_key(service, claim)["status"], "held")
        return service, brain, value, claim["run_id"]

    @staticmethod
    def verify(service, value, run_id: str) -> str:
        with service._exclusive_chat_turn("team_1", value.routine_id) as token:
            return routine_recovery.verify(service, "team_1", run_id, token)

    @staticmethod
    def resume(service, value, run_id: str) -> str:
        with service._exclusive_chat_turn("team_1", value.routine_id) as token:
            return routine_recovery.continue_run(service, "team_1", run_id, token)

    def cursor(self, service, run_id: str) -> routine_cursor.Cursor:
        return routine_incident.open_recovery(service, "team_1", run_id).cursor


class VerificationTests(RecoveryCase):
    def test_proven_absence_permits_exactly_one_retry_of_the_same_operation(self) -> None:
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held(directory, assistant)
            self.assertEqual(self.verify(service, value, run_id), "absent")
            self.assertTrue(self.cursor(service, run_id).absent)
            self.assertEqual(self.resume(service, value, run_id), "recovered")
            state = self.state(service)
        creates = [operation for action, operation in assistant.calls if action == "create-record"]
        verified = [operation for action, operation in assistant.calls if action == "find-record"]
        # The retry repeats the same logical operation the verifier proved absent; the prefix never runs again.
        self.assertEqual((len(creates), creates[0]), (2, creates[1]))
        self.assertEqual(verified, creates[:1])
        self.assertEqual([action for action, _id in assistant.calls].count("list-zones"), 1)
        self.assertEqual((brain.calls, state.incidents, state.runs), ([], (), ()))
        self.assertEqual(state.notices[-1].outcome, "recovered")

    def test_a_not_found_failure_alone_never_proves_absence(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            self.assertEqual(self.verify(service, value, run_id), "inconclusive")
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.resume(service, value, run_id)
            state = self.state(service)
        # The uncertain operation is never passed: the run stays held instead of repeating it.
        self.assertEqual(caught.exception.code, "routine-operation-uncertain")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])

    def test_a_proven_occurrence_completes_the_step_with_its_recovered_result(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            self.assertEqual(self.verify(service, value, run_id), "occurred")
            self.assertEqual(self.cursor(service, run_id).step, 2)
            self.assertEqual(self.resume(service, value, run_id), "recovered")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)

    def test_an_occurrence_without_a_valid_recovered_result_is_inconclusive(self) -> None:
        for verdict in ({"outcome": "occurred"}, {"outcome": "occurred", "result": {"record": {}}}, {"x": 1}):
            assistant = Assistant([failed()], [verdict])
            with tempfile.TemporaryDirectory() as directory, self.subTest(verdict=verdict):
                service, _brain, value, run_id = self.held(directory, assistant)
                self.assertEqual(self.verify(service, value, run_id), "inconclusive")
                self.assertEqual(self.cursor(service, run_id).step, 1)

    def test_the_one_retry_is_never_repeated_in_the_run(self) -> None:
        assistant = Assistant([failed(), failed()], [{"outcome": "not_occurred"}, {"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            self.assertEqual(self.verify(service, value, run_id), "absent")
            self.assertEqual(self.resume(service, value, run_id), "held")
            self.assertEqual(self.verify(service, value, run_id), "absent")
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.resume(service, value, run_id)
            cursor = self.cursor(service, run_id)
        self.assertEqual(caught.exception.code, "routine-retry-exhausted")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)
        self.assertEqual((cursor.remaining("retries"), cursor.segment), (0, 1))

    def test_an_automatic_verification_spends_its_budget_and_a_restart_never_refills_it(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}] * 3)
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            verdicts = [self.verify(service, value, run_id) for _attempt in range(4)]
            # A new store over the same state is a restart: the sealed cursor keeps what was spent.
            restarted = service.routine_store.__class__(service.routine_store.root, service.routine_store.key_path)
            service.routine_store = restarted
            after = self.verify(service, value, run_id)
        self.assertEqual(verdicts, ["inconclusive"] * 3 + ["exhausted"])
        self.assertEqual(after, "exhausted")

    def test_a_read_only_failure_is_absent_by_its_reviewed_declaration(self) -> None:
        def invoke(_team, _assistant, action, _payload, _evidence):
            if action == "list-zones":
                raise failed()
            return {"result": LOOKUP_RESULT}

        with tempfile.TemporaryDirectory() as directory:
            _controller, service, brain, value = self.compiled(directory, invoke)
            claim = service.claim_routine_run()
            self.assertEqual(self.run_without_key(service, claim)["status"], "held")
            self.assertEqual(self.verify(service, value, claim["run_id"]), "absent")
        self.assertEqual(brain.calls, [])

    def test_a_step_with_no_verifier_or_no_dispatch_says_so(self) -> None:
        assistant = Assistant([failed()], [])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            action = service._team_assistants("team_1")[2][ASSISTANT].spec.actions["create-record"]
            unverified = dataclasses.replace(action, verifier=None)
            with self.subTest(verifier=None):
                assessment = routine_recovery.assess(service, "team_1", run_id)
                self.assertIsNone(routine_recovery.verifier_request(dataclasses.replace(assessment, action=unverified)))
            clean = dataclasses.replace(
                self.cursor(service, run_id),
                operation_id=None,
                attempts=0,
                commitment=None,
                fault="",
                workload="",
                dispatched_at=0,
            )
            service.routine_store.put_cursor("team_1", clean)
            self.assertEqual(self.verify(service, value, run_id), "none")
