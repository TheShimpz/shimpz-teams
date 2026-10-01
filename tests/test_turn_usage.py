"""What one logical chat turn consumed: the ADR-0082 observation carried across resumes onto the completed terminal."""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
import hosted_assistant_fixture as harness
from local_controller_harness import LocalContractCase
from test_local_chat_continuations import pending
from test_local_turn_lifecycle import LOOKUP_INPUT, LOOKUP_RESULT

from action import human as action_human
from inference import client as brain_runtime_client
from inference import usage as brain_usage
from local.chat import continuation
from protocol.http.v1 import payload as http_payload

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


class LocalTurnUsageTests(LocalContractCase):
    def test_a_completed_turn_reports_every_brain_call_and_its_duration_across_a_human_resume(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)
        descriptor = {"kind": "approval", "ordinal": 0, "title": "List zones", "description": "Allow listing zones."}
        descriptor["fingerprint"] = action_human._fingerprint(descriptor)
        admitted = action_human.validate_request(descriptor, ("approval",))

        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                brain_usage.record("turn", "openai", "gpt-6-luna", REPORTED)
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def purpose(self, *_args):
                reported = {**REPORTED, "input_tokens": 9, "output_tokens": 4}
                brain_usage.record("purpose", "openai", "gpt-6-luna", reported)
                return "To list your zones, I need to read them in Cloudflare."

            def resume(self, _context, _results):
                brain_usage.record("turn-resume", "openai", "gpt-6-luna", {**REPORTED, "input_tokens": 60})
                return brain_runtime_client.RuntimeTurn("completed", "Two zones.", ())

        invocations: list[object] = []

        def invoke(*_args):
            invocations.append(_args)
            if len(invocations) == 1:
                raise action_human.HumanRequestSuspensionError(admitted)
            return {"result": LOOKUP_RESULT}

        body = {
            "message": "List zones",
            "files": [],
            "assistant_ids": ["shimpz-cloudflare"],
            "conversation": [],
            "locale": "en",
        }
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_lifecycle.invoke = invoke
            # Each Team HTTP request has its own meter; the turn's usage rides on its encrypted continuation.
            with brain_usage.metered(), _clock(1_000):
                paused = controller.chat_turn_service.chat("team_1", body, "openai", "sk-test-0123456789")
            self.assertEqual(paused["status"], "human-required")
            self.assertNotIn("usage", paused)
            with brain_usage.metered(), _clock(9_500):
                completed = controller.chat_turn_service.resume_chat_human(
                    "team_1",
                    {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    "sk-test-0123456789",
                )

        self.assertEqual(completed["reply"], "Two zones.")
        self.assertEqual(
            completed["usage"],
            {
                "duration_ms": 8_500,
                "models": [{"provider": "openai", "model": "gpt-6-luna", "input_tokens": 1400, "output_tokens": 76}],
            },
        )
        self.assertEqual(http_payload.canonical_turn_usage(completed["usage"]), completed["usage"])


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


if __name__ == "__main__":
    unittest.main()
