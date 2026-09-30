"""A Routine run's segment uses its own thread and generation in the Team's network, with read-only knowledge."""

from __future__ import annotations

import tempfile
from http import HTTPStatus
from types import SimpleNamespace

from local_controller_harness import LocalContractCase

from inference import client as brain_runtime_client
from local import app as local_app
from local.chat.segment import RoutineSegment, SegmentRequest
from local.routine import store as routine_store
from local.routine import turn as routine_turn

RUN = "f" * 32
TURN = "turn-" + "0" * 32


class Runtime:
    def __init__(self) -> None:
        self.contexts: list[brain_runtime_client.RuntimeContext] = []

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        return brain_runtime_client.RuntimeTurn(status="completed", reply="Done.", actions=())


class RoutineSegmentTests(LocalContractCase):
    def request(self, controller, routine: RoutineSegment | None) -> SegmentRequest:
        return SegmentRequest(
            team_id="team_1",
            file_ids=[],
            assistant_ids=("shimpz-cloudflare",),
            provider="openai",
            api_key="sk-test-0123456789",
            token=TURN,
            message="Every Monday, check the DNS records.",
            routine=routine,
        )

    def test_a_routine_segment_runs_in_its_own_thread_and_generation_with_read_only_knowledge(self) -> None:
        runtime = Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, runtime)
            network = controller.chat_turn_service._chat_setup("team_1", [], "openai", ("shimpz-cloudflare",))[1]
            routine = RoutineSegment(RUN, f"{network}:routine:{RUN}")
            controller.chat_turn_service._run_chat_segment(self.request(controller, routine))
            controller.chat_turn_service._run_chat_segment(self.request(controller, None))
        run, chat = runtime.contexts
        expected_thread = f"local:local-space:team_1:{network}:routine-{RUN}"
        self.assertEqual((run.thread_id, run.routines, run.knowledge_writable), (expected_thread, None, False))
        self.assertEqual((chat.routines, chat.knowledge_writable), ((), True))
        self.assertEqual(len(routine.batches), 1)

    def test_a_routine_segment_never_runs_in_another_network_or_as_another_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            network = controller.chat_turn_service._chat_setup("team_1", [], "openai", ("shimpz-cloudflare",))[1]
            for stale in (
                RoutineSegment(RUN, f"{'e' * 64}:routine:{RUN}"),
                RoutineSegment(RUN, f"{network}:routine:{'0' * 32}"),
            ):
                with self.subTest(stale=stale), self.assertRaises(local_app.ApiProblem) as caught:
                    controller.chat_turn_service._run_chat_segment(self.request(controller, stale))
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
