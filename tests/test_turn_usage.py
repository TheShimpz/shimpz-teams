"""What one logical chat turn consumed: the ADR-0082 observation carried across resumes onto the completed terminal."""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
import hosted_assistant_fixture as harness
from local_controller_harness import LocalContractCase, chat_body
from test_local_chat_continuations import pending
from test_local_turn_lifecycle import LOOKUP_INPUT, LOOKUP_RESULT

from action import challenges as action_challenges
from action import human as action_human
from inference import client as brain_runtime_client
from inference import usage as brain_usage
from local import audit as local_audit
from local.chat import api as local_chat_api
from local.chat import continuation
from local.chat import segment as local_segment
from local.routine import recorder as local_routine_recorder
from protocol.http.v1 import payload as http_payload
from tests import human_request_fixtures

REPORTED = {
    "model_calls": 1,
    "failed_calls": 0,
    "unreported_calls": 0,
    "input_tokens": 1331,
    "output_tokens": 36,
    "cache_read_tokens": 0,
    "cache_write_tokens": 1328,
}
UNCALLED = dict.fromkeys(brain_usage.FIELDS, 0)
LUNA = {"provider": "openai", "model": "gpt-6-luna", "input_tokens": 1331, "output_tokens": 36}


def _clock(*values: int):
    return mock.patch.object(brain_usage, "_now_ms", side_effect=list(values))


class TurnUsageAccountingTests(unittest.TestCase):
    def test_the_request_tally_keeps_reported_calls_and_survives_audit_drains(self) -> None:
        meter = brain_usage.Meter()
        meter.add("turn", "openai", "gpt-6-luna", REPORTED)
        meter.add("purpose", "openai", "gpt-6-luna", {**REPORTED, "input_tokens": 9, "output_tokens": 4})
        meter.add("turn-resume", "anthropic", "claude-sonnet-5-5", UNCALLED)
        self.assertIsNotNone(meter.drain())
        self.assertEqual(meter.tokens(), {("openai", "gpt-6-luna"): (1340, 40)})

    def test_joining_adds_the_current_request_to_the_carried_turn_within_the_wire_bounds(self) -> None:
        started = brain_usage.TurnUsage(1_000, (("openai", "gpt-6-luna", 10, 2),))
        self.assertEqual(started.joined(), started)
        with brain_usage.metered():
            brain_usage.record("turn-resume", "openai", "gpt-6-luna", REPORTED)
            brain_usage.record("turn", "anthropic", "claude-sonnet-5-5", REPORTED)
            joined = started.joined()
        self.assertEqual(
            joined.models,
            (("anthropic", "claude-sonnet-5-5", 1331, 36), ("openai", "gpt-6-luna", 1341, 38)),
        )
        self.assertEqual(joined.started_ms, 1_000)

        maximum = http_payload.MAX_TURN_USAGE_TOKENS
        crowded = brain_usage.TurnUsage(
            0,
            (
                ("Upper", "model", 1, 1),
                ("openai", "models/gpt", 1, 1),
                ("openai", "gpt-6-luna", maximum, maximum - 1),
                *((f"provider-{index:02d}", "model", 1, 1) for index in range(20)),
            ),
        )
        with brain_usage.metered():
            brain_usage.record("turn", "openai", "gpt-6-luna", REPORTED)
            bounded = crowded.joined()
        self.assertEqual(len(bounded.models), http_payload.MAX_TURN_USAGE_MODELS)
        self.assertEqual(bounded.models[0], ("openai", "gpt-6-luna", maximum, maximum))
        self.assertNotIn("Upper", repr(bounded.models))
        self.assertNotIn("models/gpt", repr(bounded.models))
        self.assertEqual(bounded.models[-1][0], "provider-14")

    def test_the_wire_value_is_absent_without_calls_and_clamps_its_duration(self) -> None:
        with _clock(5_000):
            started = brain_usage.TurnUsage.start()
        self.assertEqual(started, brain_usage.TurnUsage(5_000))
        self.assertIsNone(started.wire())
        used = dataclasses.replace(started, models=(("openai", "gpt-6-luna", 1331, 36),))
        with _clock(11_200, 4_000, 5_000 + http_payload.MAX_TURN_DURATION_MS + 1):
            self.assertEqual(used.wire(), {"duration_ms": 6_200, "models": [LUNA]})
            self.assertEqual(used.wire()["duration_ms"], 0)
            self.assertEqual(used.wire()["duration_ms"], http_payload.MAX_TURN_DURATION_MS)
        self.assertEqual(http_payload.canonical_turn_usage({"duration_ms": 1, "models": [LUNA]})["models"], [LUNA])
        self.assertGreater(brain_usage._now_ms(), 0)


class TurnUsageContinuationCodecTests(unittest.TestCase):
    def test_a_paused_turn_keeps_its_usage_and_a_routine_run_keeps_none(self) -> None:
        usage = brain_usage.TurnUsage(1_700_000_000_000, (("openai", "gpt-6-luna", 1331, 36),))
        for value in (None, brain_usage.TurnUsage(7), usage):
            with self.subTest(value=value):
                carried = dataclasses.replace(pending(), usage=value)
                raw = json.loads(json.dumps(continuation._pending_payload(carried)))
                self.assertEqual(continuation._pending(raw).usage, value)

    def test_malformed_usage_is_refused_both_ways(self) -> None:
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._pending_payload(dataclasses.replace(pending(), usage={"started_ms": 1}))
        raw = continuation._pending_payload(dataclasses.replace(pending(), usage=brain_usage.TurnUsage(1)))
        for usage in (
            [],
            {"started_ms": 1},
            {"started_ms": True, "models": []},
            {"started_ms": -1, "models": []},
            {"started_ms": continuation.MAX_STARTED_MS + 1, "models": []},
            {"started_ms": 1, "models": {}},
            {"started_ms": 1, "models": [{**LUNA, "input_tokens": -1}]},
            {"started_ms": 1, "models": [LUNA, LUNA]},
        ):
            value = copy.deepcopy(raw)
            value["usage"] = usage
            with self.subTest(usage=usage), self.assertRaises(continuation.ContinuationCodecError):
                continuation._pending(value)


def _approval() -> action_human.HumanRequest:
    # The harness binding's catalog carries exactly this reviewed copy (ADR-0091).
    return human_request_fixtures.request("approval", title="List zones", description="Allow listing the zones.")


def _reported(inputs: int, outputs: int) -> dict[str, int]:
    return {**REPORTED, "input_tokens": inputs, "output_tokens": outputs}


class LocalTurnUsageTests(LocalContractCase):
    def test_each_segment_counts_once_across_two_human_pauses_and_an_encrypted_reopen(self) -> None:
        first = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)
        second = brain_runtime_client.ActionRequest("action-2", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            resumes = 0

            def start(self, _context, _message, *, conversation=()):
                brain_usage.record("turn", "openai", "gpt-6-luna", _reported(100, 10))
                return brain_runtime_client.RuntimeTurn("action-required", "", (first,))

            def purpose(self, *_args):
                brain_usage.record("purpose", "openai", "gpt-6-luna", _reported(9, 4))
                return "To list your zones, I need to read them in Cloudflare."

            def resume(self, _context, _results):
                self.resumes += 1
                brain_usage.record("turn-resume", "openai", "gpt-6-luna", _reported(200 * self.resumes, 20))
                if self.resumes == 1:
                    return brain_runtime_client.RuntimeTurn("action-required", "", (second,))
                return brain_runtime_client.RuntimeTurn("completed", "Two zones.", ())

        def invoke(*args):
            # Each Action pauses once for approval, then its approved replay returns the result.
            if not args[4].transcript.responses:
                raise action_human.HumanRequestSuspensionError(_approval())
            return {"result": LOOKUP_RESULT}

        body = chat_body("List zones", assistant_ids=["shimpz-cloudflare"])
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_lifecycle.invoke = invoke
            service = controller.chat_turn_service

            def approve(paused: dict[str, object]) -> dict[str, object]:
                answer = {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True}
                return service.resume_chat_human("team_1", answer, "openai", "sk-test-0123456789")

            # Each Team HTTP request has its own meter; the turn's usage rides on its encrypted continuation.
            with brain_usage.metered(), _clock(1_000):
                paused = service.chat("team_1", {**body, "locale": "en"}, "openai", "sk-test-0123456789")
            # A Controller restart reopens the encrypted continuation with the usage so far.
            service.human_challenges = action_challenges.HumanChallengeStore()
            service._restore_all_chat_continuations()
            restored = service.human_challenges.current("team_1").payload
            self.assertEqual(restored.usage, brain_usage.TurnUsage(1_000, (("openai", "gpt-6-luna", 109, 14),)))
            with brain_usage.metered():
                paused_again = approve(paused)
            self.assertEqual(paused_again["status"], "human-required")
            carried = service.human_challenges.current("team_1").payload.usage
            self.assertEqual(carried, brain_usage.TurnUsage(1_000, (("openai", "gpt-6-luna", 318, 38),)))
            # Two Actions would teach a skill; that save is audited under a request principal this unit has not.
            with (
                brain_usage.metered(),
                _clock(50_000),
                mock.patch.object(local_chat_api.chat_knowledge, "learned_skill", return_value=None),
            ):
                completed = approve(paused_again)

        self.assertNotIn("usage", paused)
        self.assertEqual(completed["reply"], "Two zones.")
        expected = {"provider": "openai", "model": "gpt-6-luna", "input_tokens": 718, "output_tokens": 58}
        self.assertEqual(completed["usage"], {"duration_ms": 49_000, "models": [expected]})

    def test_a_routine_mode_turn_keeps_its_starting_model_after_its_span_expires(self) -> None:
        action = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)
        models: list[tuple[str, str]] = []

        class Runtime:
            # Like the Brain client, each call is metered against the model its context names.
            def start(self, context, _message, *, conversation=()):
                models.append((context.provider, context.model))
                brain_usage.record("turn", context.provider, context.model, _reported(100, 10))
                return brain_runtime_client.RuntimeTurn("action-required", "", (action,))

            def purpose(self, *_args):
                return "To list your zones, I need to read them in Cloudflare."

            def resume(self, context, _results):
                models.append((context.provider, context.model))
                brain_usage.record("turn-resume", context.provider, context.model, _reported(200, 20))
                return brain_runtime_client.RuntimeTurn("completed", "Two zones.", ())

        def invoke(*args):
            if not args[4].transcript.responses:
                raise action_human.HumanRequestSuspensionError(_approval())
            return {"result": LOOKUP_RESULT}

        later = [0]
        body = {
            **chat_body("List zones every 30 seconds", assistant_ids=["shimpz-cloudflare"], locale="en"),
            "request": {"issued_at": int(time.time()) + 1, "nonce": "0" * 32},
        }
        principal = local_audit.AuditPrincipal("a" * 32, "human")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(local_audit, "record_request", return_value="a" * 32),
            local_audit.bind_request_principal(principal),
        ):
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_lifecycle.invoke = invoke
            controller.routine_recordings = local_routine_recorder.RecordingBook(lambda: time.time() + later[0])
            service = controller.chat_turn_service
            service.routine_recordings = controller.routine_recordings
            with brain_usage.metered(), _clock(1_000):
                paused = service.chat("team_1", body, "openai", "sk-test-0123456789")
            later[0] = local_routine_recorder.SPAN_SECONDS + 1
            answer = {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True}
            with (
                brain_usage.metered(),
                _clock(50_000),
                mock.patch.object(local_chat_api.chat_knowledge, "learned_skill", return_value=None),
            ):
                completed = service.resume_chat_human("team_1", answer, "openai", "sk-test-0123456789")

        sol = local_segment.ROUTINE_OPENAI_MODEL
        self.assertEqual(models, [("openai", sol), ("openai", sol)])
        expected = {"provider": "openai", "model": sol, "input_tokens": 300, "output_tokens": 30}
        self.assertEqual(completed["usage"], {"duration_ms": 49_000, "models": [expected]})

    def test_an_integration_resume_adds_its_calls_to_the_carried_turn(self) -> None:
        carried = brain_usage.TurnUsage(1_000, (("openai", "gpt-6-luna", 100, 10),))
        challenge = SimpleNamespace(id="c" * 32)
        pending_chat = dataclasses.replace(pending(), usage=carried)
        admission = SimpleNamespace(response=None, pending=pending_chat)
        with tempfile.TemporaryDirectory() as directory:
            service = self._chat_controller(directory, SimpleNamespace()).chat_turn_service
            outcome = SimpleNamespace(outcome=None, team_name="Marketing", identity=())

            def run(_request):
                brain_usage.record("turn-resume", "openai", "gpt-6-luna", _reported(50, 5))
                return outcome

            def respond(response):
                return {"usage": response.usage.joined().wire()}

            with (
                mock.patch.object(local_chat_api.chat_turn_engine, "admit_integration_resume", return_value=admission),
                mock.patch.object(service, "_run_chat_segment", side_effect=run),
                mock.patch.object(service, "_segment_response", side_effect=respond),
                brain_usage.metered(),
                _clock(4_000),
            ):
                result = service.resume_chat_integrations(
                    "team_1", {"challenge_id": challenge.id}, "openai", "sk-test-0123456789"
                )
        expected = {"provider": "openai", "model": "gpt-6-luna", "input_tokens": 150, "output_tokens": 15}
        self.assertEqual(result["usage"], {"duration_ms": 3_000, "models": [expected]})


class HostedTurnUsageTests(unittest.TestCase):
    segment = harness.hosted_chat_segment
    orchestrator = harness.hosted_chat_segment.chat_orchestrator

    def _respond(self, outcome: object, usage: brain_usage.TurnUsage | None, requirements=((), ())) -> dict:
        identity = ("anchor", "account_1", "Marketing")
        result = self.segment.chat_turn_engine.SegmentResult("Marketing", identity, outcome, *requirements)
        with mock.patch.object(harness.runtime_state, "_commit_chat_terminal", return_value=True):
            return self.segment._hosted_segment_response(
                self.segment.HostedSegmentResponseRequest("team_1", "token", result, (), (), "account_1", usage=usage)
            )

    def test_a_completed_hosted_turn_carries_its_usage_only_when_a_call_reported_it(self) -> None:
        outcome = self.orchestrator.ChatOutcome("Done.", ())
        with brain_usage.metered(), _clock(3_000):
            brain_usage.record("turn", "openai", "gpt-6-luna", REPORTED)
            completed = self._respond(outcome, brain_usage.TurnUsage(1_000))
        self.assertEqual(completed["usage"], {"duration_ms": 2_000, "models": [LUNA]})
        with brain_usage.metered():
            self.assertNotIn("usage", self._respond(outcome, brain_usage.TurnUsage(1_000)))
            self.assertNotIn("usage", self._respond(outcome, None))

    def test_a_paused_hosted_turn_keeps_its_joined_usage_for_the_resume(self) -> None:
        captured: list[object] = []
        outcome = self.orchestrator.ChatHumanSuspension(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

        def dispatch(_outcome, _groups, pending, _pauses, _complete):
            captured.append(pending(outcome))
            return {"status": "human-required"}

        with (
            mock.patch.object(self.segment.chat_turn_engine, "dispatch", side_effect=dispatch),
            mock.patch.object(self.orchestrator, "retain_suspension_transcripts", return_value=()),
            brain_usage.metered(),
        ):
            brain_usage.record("turn", "openai", "gpt-6-luna", REPORTED)
            self._respond(outcome, brain_usage.TurnUsage(1_000))
            self._respond(outcome, None)
        self.assertEqual(captured[0].usage, brain_usage.TurnUsage(1_000, (("openai", "gpt-6-luna", 1331, 36),)))
        self.assertIsNone(captured[1].usage)

    def test_a_fresh_hosted_turn_starts_its_clock_at_admission(self) -> None:
        usage = self.segment.brain_usage
        with (
            mock.patch.object(usage, "_now_ms", return_value=42),
            mock.patch.object(self.segment, "_run_hosted_chat_segment", return_value="segment"),
            mock.patch.object(self.segment, "_hosted_segment_response", return_value={}) as respond,
        ):
            turn = "turn-1"
            request = SimpleNamespace(team_id="team_1", token=turn, file_ids=[], assistant_ids=(), owner="account_1")
            self.segment._chat_in_turn(request)
        self.assertEqual(respond.call_args.args[0].usage, usage.TurnUsage(42))


class HostedTurnUsageResumeTests(unittest.TestCase):
    """The Hosted human and Integration resume entrypoints add each segment once to the carried turn."""

    segment = harness.hosted_chat_segment
    usage = harness.hosted_chat_segment.brain_usage
    orchestrator = harness.hosted_chat_segment.chat_orchestrator

    def _pending(self, usage: object) -> object:
        return harness.hosted_assistants._PendingHostedChat(
            SimpleNamespace(), (), (), "account_1", ("anchor",), (), 0, usage=usage
        )

    def _segment(self, inputs: int, *, paused: bool) -> object:
        def run(_request):
            self.usage.record("turn-resume", "openai", "gpt-6-luna", _reported(inputs, 1))
            if paused:
                outcome = self.orchestrator.ChatHumanSuspension(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
                return self.segment.chat_turn_engine.SegmentResult("Marketing", (), outcome, (), (object(),))
            return self.segment.chat_turn_engine.SegmentResult(
                "Marketing", (), self.orchestrator.ChatOutcome("Done.", ()), ()
            )

        return mock.patch.object(self.segment, "_run_hosted_chat_segment", side_effect=run)

    @staticmethod
    @contextmanager
    def _exclusive(_team_id, _lease):
        yield "turn-1", SimpleNamespace(id="anchor")

    def _resume_human(self, pending: object) -> dict[str, object]:
        human = harness.hosted_chat_human
        with (
            mock.patch.object(human, "_pending_challenge", return_value=SimpleNamespace()),
            mock.patch.object(human, "_validate_pending_context", return_value=pending),
            mock.patch.object(human, "_admit_response", return_value=SimpleNamespace(transcripts=(), requests_used=1)),
        ):
            body = {"challenge_id": "c" * 32, "decision": "submit", "value": True}
            return human.resume_chat_human("team_1", body, None, SimpleNamespace(owner="account_1"), self._exclusive)

    def test_repeated_human_resumes_keep_the_start_and_add_each_segment_once(self) -> None:
        paused: list[object] = []

        def pause(_team_id, _token, _outcome, _requirements, pending):
            paused.append(pending)
            return {"status": "human-required"}

        carried = self.usage.TurnUsage(1_000, (("openai", "gpt-6-luna", 100, 10),))
        with (
            mock.patch.object(self.segment, "_pause_hosted_human", side_effect=pause),
            mock.patch.object(self.orchestrator, "retain_suspension_transcripts", return_value=()),
            mock.patch.object(harness.runtime_state, "_commit_chat_terminal", return_value=True),
            mock.patch.object(self.usage, "_now_ms", return_value=7_000),
        ):
            with self.usage.metered(), self._segment(20, paused=True):
                self.assertEqual(self._resume_human(self._pending(carried)), {"status": "human-required"})
            with self.usage.metered(), self._segment(30, paused=False):
                completed = self._resume_human(paused[0])

        self.assertEqual(paused[0].usage, self.usage.TurnUsage(1_000, (("openai", "gpt-6-luna", 120, 11),)))
        expected = {"provider": "openai", "model": "gpt-6-luna", "input_tokens": 150, "output_tokens": 12}
        self.assertEqual(completed["usage"], {"duration_ms": 6_000, "models": [expected]})

    def test_an_integration_resume_adds_its_calls_to_the_carried_turn(self) -> None:
        api = harness.hosted_chat_api
        carried = self.usage.TurnUsage(2_000, (("openai", "gpt-6-luna", 100, 10),))
        admission = SimpleNamespace(response=None, pending=self._pending(carried))
        with (
            mock.patch.object(api, "_exclusive_chat_turn", self._exclusive),
            mock.patch.object(api.chat_turn_engine, "admit_integration_resume", return_value=admission),
            mock.patch.object(harness.runtime_state, "_commit_chat_terminal", return_value=True),
            mock.patch.object(self.usage, "_now_ms", return_value=2_500),
            self.usage.metered(),
            self._segment(5, paused=False),
        ):
            completed = api._resume_chat_integrations("team_1", "c" * 32, SimpleNamespace(owner="account_1"))

        expected = {"provider": "openai", "model": "gpt-6-luna", "input_tokens": 105, "output_tokens": 11}
        self.assertEqual(completed["usage"], {"duration_ms": 500, "models": [expected]})


if __name__ == "__main__":
    unittest.main()
