"""A held run's one automatic recovery episode: verification first, the Brain only on proven absence (ADR-0092)."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

from local_assistant_fixture import mutating_spec
from test_local_chat_scope import LOOKUP_INPUT
from test_local_routine_compiled import ZONE
from test_local_routine_recovery import RECORD, Assistant, RecoveryCase, failed
from test_local_routine_service import API_KEY, ASSISTANT, KEY

from inference import client as inference_client
from inference import recovery as inference_recovery
from local import authority as local_authority
from local.routine import recovery as routine_recovery
from routine import record


class Brain:
    """The Brain as the automatic episode may reach it: only its one recovery decision, each answer in turn."""

    def __init__(self, *decisions: str) -> None:
        self.decisions = list(decisions)
        self.asked: list[dict[str, object]] = []

    def routine_recovery(self, payload, _provider, _model):
        self.asked.append(payload)
        return {"decision": self.decisions.pop(0)}

    def __getattr__(self, name: str):
        raise AssertionError(f"the episode asked the Brain to {name}")


class AutomaticCase(RecoveryCase):
    def run_held(self, directory: str, assistant: Assistant, brain: Brain, key: str = API_KEY):
        """Claim and run with the model key Admin sends; the hold runs its automatic episode at once."""
        service, _brain, value, run_id = self.held_automatic(directory, assistant, brain, key)
        return service, value, run_id

    def held_automatic(self, directory, assistant, brain, key):
        controller, service = self.service(directory, brain)
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
        claim = service.claim_routine_run(("anthropic", "openai"))
        evidence = local_authority.RoutineEvidence(KEY, record.lease_sha256(claim["lease_token"]), "a" * 32, 0)
        self.status = service.run_routine(
            "team_1", claim["run_id"], evidence, (claim["revision"], claim["plan_digest"]), ("openai", key)
        )["status"]
        return service, brain, value, claim["run_id"]


class AutomaticTests(AutomaticCase):
    def test_proven_absence_asks_once_and_retries_the_same_operation(self) -> None:
        brain = Brain("retry")
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, _run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
        self.assertEqual((self.status, state.incidents, state.runs), ("recovered", (), ()))
        (asked,) = brain.asked
        self.assertEqual(
            (asked["proof"], asked["step"]), ("not_occurred", {"assistant": ASSISTANT, "action": "create-record"})
        )
        # The step's sanitized diagnostic is the Brain's untrusted evidence; the 404 alone never proved anything.
        self.assertEqual(asked["diagnostics"][0]["failure"]["http_status"], 404)
        creates = [operation for action, operation in assistant.calls if action == "create-record"]
        self.assertEqual((len(creates), creates[0]), (2, creates[1]))

    def test_a_proven_occurrence_continues_with_no_model_call(self) -> None:
        brain = Brain()
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, _run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
        self.assertEqual((self.status, brain.asked, state.incidents), ("recovered", [], ()))

    def test_inconclusive_evidence_holds_for_the_card_without_asking_the_brain(self) -> None:
        brain = Brain()
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        with tempfile.TemporaryDirectory() as directory:
            service, value, run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
            cursor = self.cursor(service, run_id)
        self.assertEqual((self.status, brain.asked), ("held", []))
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        self.assertFalse(record.routine(state, value.routine_id).paused)
        self.assertEqual((cursor.remaining("episodes"), cursor.remaining("verifications")), (0, 2))

    def test_ask_holds_and_pause_or_an_unavailable_decision_also_pauses_the_routine(self) -> None:
        cases = ((("ask",), API_KEY, None), (("pause",), API_KEY, "decided"), ((), "", "unavailable"))
        for decisions, key, reason in cases:
            paused = reason is not None
            brain = Brain(*decisions)
            assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
            with tempfile.TemporaryDirectory() as directory, self.subTest(decisions=decisions, key=bool(key)):
                service, value, run_id = self.run_held(directory, assistant, brain, key)
                state = self.state(service)
                cursor = self.cursor(service, run_id)
                self.assertEqual(self.status, "held")
                self.assertEqual(record.routine(state, value.routine_id).paused, paused)
                # The held run's one notice says it is held, or why recovery paused its Routine.
                notice = state.notices[-1]
                step = {"assistant_id": ASSISTANT, "action": "create-record"}
                expected = ("held", step) if reason is None else ("paused", {**step, "reason": reason})
                self.assertEqual((notice.notice_id, (notice.outcome, notice.detail)), (run_id, expected))
                # Each call and its whole output cap are paid before the call; with no key, nothing is asked.
                self.assertEqual(len(brain.asked), len(decisions))
                self.assertEqual((cursor.remaining("model_calls"), cursor.remaining("output_tokens")), (3, 3072))

    def test_the_episode_runs_once_and_the_retry_never_repeats(self) -> None:
        brain = Brain("retry", "retry")
        assistant = Assistant([failed(), failed()], [{"outcome": "not_occurred"}, {"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.run_held(directory, assistant, brain)
            cursor = self.cursor(service, run_id)
        self.assertEqual((self.status, len(brain.asked)), ("held", 1))
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)
        self.assertEqual((cursor.remaining("episodes"), cursor.remaining("retries")), (0, 0))

    def test_an_episode_past_its_active_time_stops_and_a_restart_never_starts_another(self) -> None:
        brain = Brain("retry")
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        ticks = iter([0.0])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)):
                service, _value, run_id = self.run_held(directory, assistant, brain)
            cursor = self.cursor(service, run_id)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            again = routine_recovery.automatic(service, run, API_KEY)
        self.assertEqual((self.status, brain.asked, again), ("held", [], "held"))
        self.assertEqual(cursor.remaining("recovery_seconds"), 0)


class AutomaticEdgeTests(AutomaticCase):
    def test_an_exhausted_or_unconfigured_decision_pauses_and_unreadable_diagnostics_are_left_out(self) -> None:
        for name in ("exhausted", "diagnostics"):
            brain = Brain("ask")
            assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
            patches = {
                "exhausted": mock.patch.object(
                    routine_recovery.routine_cursor, "spend", side_effect=_spend_without_calls
                ),
                "diagnostics": mock.patch.object(
                    routine_recovery.routine_diagnostics.DiagnosticStore,
                    "read",
                    side_effect=routine_recovery.routine_diagnostics.DiagnosticStoreError("down"),
                ),
            }
            with tempfile.TemporaryDirectory() as directory, self.subTest(name=name), patches[name]:
                service, value, _run_id = self.run_held(directory, assistant, brain)
                state = self.state(service)
                paused = record.routine(state, value.routine_id).paused
                self.assertEqual(self.status, "held")
                self.assertEqual(paused, name != "diagnostics")
                if name == "exhausted":
                    self.assertEqual(state.notices[-1].detail["reason"], "exhausted")
                if name == "diagnostics":
                    self.assertEqual(brain.asked[0]["diagnostics"], [])

    def test_an_unconfigured_team_has_no_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            down = routine_recovery.inference_config.InferenceConfigError("down")
            with mock.patch.object(service.inference_store, "load", side_effect=down):
                self.assertEqual(routine_recovery._decide(service, "team_1", run_id, API_KEY, None), "unavailable")

    def test_time_running_out_after_evidence_or_a_failing_assessment_holds_the_run(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        ticks = iter([0.0])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)):
                service, _value, run_id = self.run_held(directory, assistant, Brain())
            self.assertEqual(self.status, "held")
            self.assertEqual(self.cursor(service, run_id).step, 2)
        assistant = Assistant([failed()], [])
        drift = routine_recovery.ApiProblem(409, "x", code="routine-drift")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "verify", side_effect=drift),
        ):
            self.run_held(directory, assistant, Brain())
        self.assertEqual(self.status, "held")
        with mock.patch.object(routine_recovery.routine_incident, "open_recovery", side_effect=drift):
            self.assertFalse(routine_recovery._within(None, "team_1", "a" * 32, 0.0))
            self.assertIsNone(routine_recovery._spend_time(None, "team_1", "a" * 32, 0.0))

    def test_an_episode_with_no_time_left_charges_nothing_more(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.run_held(
                directory, Assistant([failed()], [{"outcome": "inconclusive"}]), Brain()
            )
            cursor = self.cursor(service, run_id)
            spent = routine_recovery.routine_cursor.spend(
                cursor, "recovery_seconds", cursor.remaining("recovery_seconds")
            )
            service.routine_store.put_cursor("team_1", spent)
            routine_recovery._spend_time(service, "team_1", run_id, 0.0)
            self.assertEqual(self.cursor(service, run_id).remaining("recovery_seconds"), 0)


_REAL_SPEND = routine_recovery.routine_cursor.spend


def _spend_without_calls(cursor, budget, amount):
    if budget == "model_calls":
        raise routine_recovery.routine_cursor.CursorError("cursor-budget-exhausted")
    return _REAL_SPEND(cursor, budget, amount)


class DecisionClientTests(unittest.TestCase):
    SUBJECT: ClassVar[dict[str, object]] = {
        "routine": {"name": "N", "request": "R"},
        "step": {"assistant": "a", "action": "b"},
        "proof": "no_effect",
    }

    def client(self, answer):
        client = inference_client.BrainRuntimeClient.__new__(inference_client.BrainRuntimeClient)
        usage = dict.fromkeys(inference_client.brain_usage.FIELDS, 0)
        client._post = mock.Mock(return_value={**answer, "usage": usage})
        return client

    def test_only_a_closed_decision_from_a_valid_request_is_admitted(self) -> None:
        client = self.client({"decision": "retry"})
        with mock.patch.object(inference_client.brain_usage, "record"):
            decided = inference_recovery.decide(client, ("openai", "gpt-6-luna", "sk-x"), "pt", self.SUBJECT, ())
            self.assertEqual(decided, "retry")
            self.assertEqual(client._post.call_args.args[0], "/v1/routine-recovery")
            for answer in ({"decision": "verify"}, {"decision": "ask", "x": 1}):
                with self.subTest(answer=answer), self.assertRaises(inference_client.BrainRuntimeError):
                    inference_recovery.decide(self.client(answer), ("openai", "m", "k"), None, self.SUBJECT, ())
        for credentials, locale, subject, diagnostics in (
            (("other", "m", "k"), None, self.SUBJECT, ()),
            (("openai", "bad model", "k"), None, self.SUBJECT, ()),
            (("openai", "m", ""), None, self.SUBJECT, ()),
            (("openai", "m", "k"), "xx", self.SUBJECT, ()),
            (("openai", "m", "k"), None, {**self.SUBJECT, "proof": "occurred"}, ()),
            (("openai", "m", "k"), None, self.SUBJECT, ({},) * 9),
        ):
            with (
                self.subTest(credentials=credentials, locale=locale),
                self.assertRaises(inference_client.BrainRuntimeError),
            ):
                inference_recovery.decide(client, credentials, locale, subject, diagnostics)
