from __future__ import annotations

import unittest

from test_chat_orchestrator import FakeRuntime, completed, context, strategy, suspended

from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress


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
                ("power-result", "started"),
                ("power-result", "finished"),
                ("reply-validation", "started"),
                ("reply-validation", "finished"),
            ],
        )
        power_events = [event for event in events if event["phase"] == "power"]
        self.assertTrue(all(event["index"] == event["total"] == 1 for event in power_events))


if __name__ == "__main__":
    unittest.main()
