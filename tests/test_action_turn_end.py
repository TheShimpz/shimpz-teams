"""A turn that fails after its Actions completed ends its batch, so the next turn's batch runs (Local generation)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_controller_harness import LocalContractCase

from action import journal as action_journal
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from local.chat.segment import SegmentRequest
from local.errors import ApiProblemError

ZONES = {"zones": [], "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0}}


def _request(interrupt_id: str) -> brain_runtime_client.ActionRequest:
    return brain_runtime_client.ActionRequest(
        interrupt_id, "shimpz-cloudflare", "list-zones", {"page": 1, "per_page": 25}
    )


class _Runtime:
    """Each turn asks for one fresh interrupt; the first resume runs ``fail``, as a provider failure or a Stop."""

    def __init__(self, fail) -> None:
        self.fail = fail
        self.turns = 0

    def start(self, _context, _message, *, conversation=()):
        self.turns += 1
        return brain_runtime_client.RuntimeTurn("action-required", "", (_request(f"call-{self.turns}"),))

    def resume(self, _context, _results):
        if self.turns == 1:
            self.fail()
        return brain_runtime_client.RuntimeTurn("completed", "Done", ())


class ActionTurnEndTests(LocalContractCase):
    def _controller(self, directory: str, fail):
        controller = self._chat_controller(directory, _Runtime(fail))
        invoked: list[str] = []

        def invoke(_team_id, _token, request, *_args):
            invoked.append(request.interrupt_id)
            return ZONES

        controller.chat_turn_service._invoke_chat_action = invoke
        return controller, invoked

    @staticmethod
    def _segment(controller, token: str):
        return controller.chat_turn_service._run_chat_segment(
            SegmentRequest(
                team_id="team_1",
                file_ids=[],
                assistant_ids=("shimpz-cloudflare",),
                provider="openai",
                api_key="test-api-key",
                token=token,
                message="List my Cloudflare zones",
            )
        )

    def _assert_next_batch_runs(self, controller, invoked: list[str]) -> None:
        result = self._segment(controller, "turn-2")
        self.assertIsInstance(result.outcome, chat_orchestrator.ChatOutcome)
        self.assertEqual(result.outcome.reply, "Done")
        self.assertEqual(invoked, ["call-1", "call-2"])

    def test_a_provider_failure_after_completed_actions_admits_the_next_batch(self) -> None:
        def provider_failure() -> None:
            raise brain_runtime_client.BrainRuntimeError("model provider request failed")

        with tempfile.TemporaryDirectory() as directory:
            controller, invoked = self._controller(directory, provider_failure)
            with self.assertRaises(ApiProblemError):
                self._segment(controller, "turn-1")
            self._assert_next_batch_runs(controller, invoked)

    def test_a_stop_after_completed_actions_admits_the_next_batch(self) -> None:
        controllers = []

        def stop() -> None:
            controllers[0].chat_turn_service._cancelled_chat_tokens.add("turn-1")
            raise brain_runtime_client.BrainRuntimeError("aborted by Stop")

        with tempfile.TemporaryDirectory() as directory:
            controller, invoked = self._controller(directory, stop)
            controllers.append(controller)
            with self.assertRaises(ApiProblemError) as stopped:
                self._segment(controller, "turn-1")
            self.assertEqual(stopped.exception.code, "chat-stopped")
            self._assert_next_batch_runs(controller, invoked)

    def test_a_restart_after_completed_actions_admits_the_next_batch(self) -> None:
        def crash() -> None:
            raise SystemExit("Controller stopped before the Brain resumed")

        with tempfile.TemporaryDirectory() as directory:
            controller, invoked = self._controller(directory, crash)
            with self.assertRaises(SystemExit):
                self._segment(controller, "turn-1")
            path = controller.action_state.path
            controller.action_state.close()
            restarted = action_journal.ActionJournal(path)
            self.addCleanup(restarted.close)
            controller.action_state = restarted
            controller.chat_turn_service.action_state = restarted
            self._assert_next_batch_runs(controller, invoked)


if __name__ == "__main__":
    unittest.main()
