"""A Team-detected policy fault holds a run: never absence, never a retry, never a model (ADR-0092 section 6)."""

from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus

from test_local_routine_automatic import AutomaticCase, Brain
from test_local_routine_recovery import RECORD, Assistant, failed

from action import execution as action_execution
from local import app as local_app
from local import audit as local_audit
from local.routine import compiled as routine_compiled
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from routine import record


def problem(code: str, cause: BaseException | None = None) -> local_app.ApiProblem:
    try:
        raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "refused", code=code) from cause
    except local_app.ApiProblem as exc:
        return exc


class ReadOnlyFault(Assistant):
    """The read-only first step fails with ``fault``; the mutating step is never reached."""

    def __init__(self, fault: BaseException) -> None:
        super().__init__([RECORD], [])
        self.fault = fault

    def __call__(self, team, assistant, action, payload, evidence):
        if action == "list-zones":
            self.calls.append((action, evidence.operation_id))
            raise self.fault
        return super().__call__(team, assistant, action, payload, evidence)


class ClassificationTests(unittest.TestCase):
    def test_only_team_admitted_evidence_classifies_a_failed_attempt(self) -> None:
        cases = {
            "policy": (
                problem("assistant-secret-exposure"),
                problem("invalid-action-output"),
                problem("assistant-rpc-failed", action_execution.RpcExchangeError("invalid-result")),
                problem("assistant-rpc-failed", action_execution.RpcExchangeError("failed", "frame-invalid")),
            ),
            "transport": (
                problem("assistant-timeout", action_execution.RpcExchangeError("timeout")),
                problem("assistant-rpc-failed", action_execution.RpcExchangeError("failed", "exit-status:1")),
            ),
            "unquiesced": (problem("assistant-action-blocked"),),
            "handled": (failed(),),
            "other": (problem("team-context-changed"), RuntimeError("x")),
        }
        # A cause chain deeper than Team ever raises is never searched further.
        deep = action_execution.RpcExchangeError("failed", "frame-invalid")
        for _depth in range(9):
            deep = problem("assistant-rpc-failed", deep)
        cases["other"] = (*cases["other"], deep)
        for fault, problems in cases.items():
            for item in problems:
                with self.subTest(fault=fault, problem=item):
                    self.assertEqual(routine_compiled.fault_of(item), fault)


class PolicyHoldTests(AutomaticCase):
    def test_a_read_only_secret_echo_is_held_for_policy_never_verified_away_or_retried(self) -> None:
        brain = Brain("retry")
        assistant = ReadOnlyFault(problem("assistant-secret-exposure"))
        with tempfile.TemporaryDirectory() as directory:
            service, value, run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
            cursor = routine_incident.open_recovery(service, "team_1", run_id).cursor
            with service._exclusive_chat_turn("team_1", value.routine_id) as token:
                manual = routine_recovery.verify(service, "team_1", run_id, token, budgeted=False)
            refused = routine_recovery.refusal(routine_incident.open_recovery(service, "team_1", run_id).cursor)
            with local_audit.bind_request_principal(local_audit.AuditPrincipal("a" * 32, "human")):
                card = service.open_routine_card("team_1", run_id)
                answered = service.answer_routine_card("team_1", run_id, {"nonce": card["nonce"], "choice": "verify"})
        # The automatic episode paused at once without asking the Brain; nothing ran twice.
        self.assertEqual((self.status, brain.asked, cursor.fault, cursor.absent), ("held", [], "policy", False))
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "policy"))
        self.assertTrue(record.routine(state, value.routine_id).paused)
        self.assertEqual([action for action, _id in assistant.calls], ["list-zones"])
        # A person's Verificar cannot admit absence or continue either; the card recommends Pausar.
        self.assertEqual((manual, refused), ("policy", "routine-policy-hold"))
        self.assertEqual((card["recommended"], answered["verdict"], answered["status"]), ("pause", "policy", None))

    def test_a_read_only_handled_failure_is_still_proven_absent(self) -> None:
        assistant = ReadOnlyFault(failed())
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            with service._exclusive_chat_turn("team_1", value.routine_id) as token:
                verdict = routine_recovery.verify(service, "team_1", run_id, token, budgeted=False)
            cursor = routine_incident.open_recovery(service, "team_1", run_id).cursor
        self.assertEqual((verdict, cursor.fault, cursor.absent), ("absent", "handled", True))
