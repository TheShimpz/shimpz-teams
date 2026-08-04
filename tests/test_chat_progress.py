from __future__ import annotations

import unittest

from test_chat_orchestrator import FakeRuntime, completed, context, strategy, suspended

from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from protocol.http.v1 import progress as progress_contract


class ChatProgressTests(unittest.TestCase):
    def test_reporter_emits_only_closed_measured_operation_pairs(self) -> None:
        events: list[dict[str, object]] = []
        ticks = iter((1_000_000_000, 1_012_000_000))
        reporter = chat_progress.Reporter(events.append, lambda: next(ticks))

        with reporter.span("power", index=1, total=2):
            pass

        self.assertEqual(
            events,
            [
                {"seq": 1, "phase": "power", "state": "started", "index": 1, "total": 2},
                {
                    "seq": 2,
                    "phase": "power",
                    "state": "finished",
                    "elapsed_ms": 12,
                    "index": 1,
                    "total": 2,
                },
            ],
        )

    def test_progress_sink_failure_never_changes_the_turn(self) -> None:
        reporter = chat_progress.Reporter(lambda _event: (_ for _ in ()).throw(OSError("closed")))

        with reporter.span("model"):
            pass

        self.assertEqual(reporter.sequence, 2)

    def test_reporter_never_emits_an_unpaired_span_at_its_sequence_cap(self) -> None:
        events: list[dict[str, object]] = []
        reporter = chat_progress.Reporter(events.append, sequence=chat_progress.MAX_SEQUENCE - 1)

        with reporter.span("model"):
            pass

        self.assertEqual(events, [])
        self.assertEqual(reporter.sequence, chat_progress.MAX_SEQUENCE - 1)

    def test_terminal_framing_accepts_the_largest_multibyte_public_reply(self) -> None:
        reply = "\U0001f642" * 60_000

        encoded = progress_contract.encode_record(
            {
                "type": "terminal",
                "status": 200,
                "body": {
                    "team_id": "a" * 40,
                    "team_name": "T" * 80,
                    "reply": reply,
                    "trace_id": "f" * 32,
                },
            }
        )

        self.assertLessEqual(len(encoded), progress_contract.MAX_LINE_BYTES)
        self.assertEqual(progress_contract.decode_line(encoded)["body"]["reply"], reply)

    def test_orchestrator_reports_real_model_power_and_validation_operations(self) -> None:
        events: list[dict[str, object]] = []
        reporter = chat_progress.Reporter(events.append)
        runtime = FakeRuntime([suspended(), completed("Finished")])

        outcome = chat_orchestrator.run(
            runtime,
            context(),
            "Run the Power",
            strategy(
                lambda _assistant, _power, payload: payload,
                lambda _request: {"ok": True},
                progress=reporter,
            ),
        )

        self.assertEqual(outcome.reply, "Finished")
        self.assertEqual(
            [(event["phase"], event["state"]) for event in events],
            [
                ("model", "started"),
                ("model", "finished"),
                ("power-preparation", "started"),
                ("power-preparation", "finished"),
                ("power", "started"),
                ("power", "finished"),
                ("model", "started"),
                ("model", "finished"),
                ("power-delivery", "started"),
                ("power-delivery", "finished"),
                ("team-context", "started"),
                ("team-context", "finished"),
            ],
        )
        power_events = [event for event in events if event["phase"] == "power"]
        self.assertTrue(all(event["index"] == event["total"] == 1 for event in power_events))


if __name__ == "__main__":
    unittest.main()
