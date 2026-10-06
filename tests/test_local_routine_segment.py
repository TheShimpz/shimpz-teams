"""A compiled Routine run's segment runs in its own generation through Team's turn loop, never through the Brain."""

from __future__ import annotations

import tempfile
import time
from http import HTTPStatus
from types import SimpleNamespace

from local_controller_harness import LocalContractCase

from inference import client as brain_runtime_client
from local import app as local_app
from local.chat.segment import RoutineSegment, SegmentRequest
from local.routine import store as routine_store
from local.routine import turn as routine_turn
from routine.request import Request as RoutineRequest

RUN = "f" * 32
TURN = "turn-" + "0" * 32


class Runtime:
    """The Brain, recording every turn it is asked for."""

    def __init__(self) -> None:
        self.contexts: list[brain_runtime_client.RuntimeContext] = []

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        return brain_runtime_client.RuntimeTurn(status="completed", reply="Done.", actions=())


class Compiled(Runtime):
    """A compiled run's runtime: its plan has no step left."""

    def dispatching(self, _request, _operation_id, _workload="") -> None:
        raise AssertionError("nothing is dispatched")

    @staticmethod
    def logical_operation(_request) -> None:
        return None

    @staticmethod
    def strategy() -> dict[str, object]:
        return {"max_rounds": 1, "record_invoked": False}


class RoutineSegmentTests(LocalContractCase):
    def request(self, routine: RoutineSegment | None) -> SegmentRequest:
        message = "Every Monday, check the DNS records."
        return SegmentRequest(
            team_id="team_1",
            file_ids=[],
            assistant_ids=("shimpz-cloudflare",),
            provider="openai",
            api_key="" if routine is not None else "test-api-key",
            token=TURN,
            message=message,
            routine=routine,
            routine_request=None
            if routine is not None
            else RoutineRequest("a" * 32, message, int(time.time()), "b" * 32),
        )

    def test_a_routine_segment_runs_its_compiled_runtime_in_its_own_generation_without_knowledge(self) -> None:
        brain, compiled = Runtime(), Compiled()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, brain)
            network = controller.chat_turn_service._chat_setup("team_1", [], "openai", ("shimpz-cloudflare",))[1]
            routine = RoutineSegment(RUN, f"{network}:routine:{RUN}", compiled)
            run = controller.chat_turn_service._run_chat_segment(self.request(routine))
            controller.chat_turn_service._run_chat_segment(self.request(None))
        (context,) = compiled.contexts
        (chat,) = brain.contexts
        expected_thread = f"local:local-space:team_1:{network}:routine-{RUN}"
        self.assertEqual(
            (context.thread_id, context.routines, context.memories, context.skills, context.api_key),
            (expected_thread, None, None, None, ""),
        )
        self.assertEqual((chat.routines, chat.knowledge_writable), ((), True))
        self.assertEqual((run.outcome.reply, len(routine.batches)), ("Done.", 1))

    def test_a_routine_segment_never_runs_in_another_network_or_as_another_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            network = controller.chat_turn_service._chat_setup("team_1", [], "openai", ("shimpz-cloudflare",))[1]
            for stale in (
                RoutineSegment(RUN, f"{'e' * 64}:routine:{RUN}", Compiled()),
                RoutineSegment(RUN, f"{network}:routine:{'0' * 32}", Compiled()),
            ):
                with self.subTest(stale=stale), self.assertRaises(local_app.ApiProblem) as caught:
                    controller.chat_turn_service._run_chat_segment(self.request(stale))
                self.assertEqual(
                    (caught.exception.status, caught.exception.code), (HTTPStatus.CONFLICT, "team-context-changed")
                )

    def test_unavailable_routine_state_fails_a_chat_turn_closed(self) -> None:
        def broken(_team_id):
            raise routine_store.RoutineStoreError("unavailable")

        subject = SimpleNamespace(routine_store=SimpleNamespace(load=broken))
        with self.assertRaises(local_app.ApiProblem) as caught:
            routine_turn.chat_routines(subject, "team_1")
        self.assertEqual(caught.exception.code, "routine-state-unavailable")
