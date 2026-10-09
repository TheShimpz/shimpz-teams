"""A Routine run's segment runs in its own generation through Team's turn loop; a new chat turn records (ADR-0101)."""

import tempfile
import time
from http import HTTPStatus
from types import SimpleNamespace

from local_controller_harness import LOOKUP_INPUT, LOOKUP_RESULT, LocalContractCase
from test_local_routine_service import LIST, acting, completed
from test_local_routine_service import Runtime as ScriptedRuntime

from inference import client as brain_runtime_client
from local import app as local_app
from local.chat.segment import RoutineSegment, SegmentRequest
from local.routine import proposal as routine_proposal
from local.routine import recorder as routine_recorder
from local.routine import store as routine_store

RUN = "f" * 32
TURN = "turn-" + "0" * 32
PRINCIPAL = "c" * 32
INCARNATION = "a" * 64


class Runtime:
    """The Brain, recording every turn it is asked for."""

    def __init__(self) -> None:
        self.contexts: list[brain_runtime_client.RuntimeContext] = []

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        return brain_runtime_client.RuntimeTurn(status="completed", reply="Done.", actions=())


class Compiled(Runtime):
    """A recorded run's runtime: its plan has no step left."""

    def dispatching(self, _request, _operation_id, _workload="") -> None:
        raise AssertionError("nothing is dispatched")

    @staticmethod
    def logical_operation(_request) -> None:
        return None

    @staticmethod
    def strategy() -> dict[str, object]:
        return {"max_rounds": 1, "record_invoked": False}

    @staticmethod
    def protect(_values) -> None:
        return None


def run_chat(service, recording: str | None) -> object:
    """One new chat turn's segment in the Team's execution slot, as the chat API runs it."""
    with service._exclusive_chat_turn("team_1") as token:
        request = SegmentRequest(
            team_id="team_1",
            file_ids=[],
            assistant_ids=("shimpz-cloudflare",),
            provider="openai",
            api_key="test-api-key",
            token=token,
            message="List my zones",
            recording=recording,
        )
        return service._run_chat_segment(request)


class RoutineSegmentTests(LocalContractCase):
    def request(self, routine: RoutineSegment | None, recording: str | None = None) -> SegmentRequest:
        return SegmentRequest(
            team_id="team_1",
            file_ids=[],
            assistant_ids=("shimpz-cloudflare",),
            provider="openai",
            api_key="" if routine is not None else "test-api-key",
            token=TURN,
            message="Every Monday, check the DNS records.",
            routine=routine,
            recording=recording,
        )

    def test_a_routine_segment_runs_its_runtime_in_its_own_generation_without_knowledge(self) -> None:
        brain, compiled = Runtime(), Compiled()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, brain)
            network = controller.chat_turn_service._chat_setup("team_1", [], "openai", ("shimpz-cloudflare",))[1]
            routine = RoutineSegment(RUN, f"{network}:routine:{RUN}", compiled)
            run = controller.chat_turn_service._run_chat_segment(self.request(routine))
            controller.chat_turn_service._run_chat_segment(self.request(None, "b" * 32))
        (context,) = compiled.contexts
        (chat,) = brain.contexts
        expected_thread = f"local:local-space:team_1:{network}:routine-{RUN}"
        self.assertEqual(
            (context.thread_id, context.routines, context.memories, context.skills, context.api_key),
            (expected_thread, None, None, None, ""),
        )
        # Only a recorded new turn sees the Team's Routines and the Routine tool.
        self.assertEqual((chat.routines, chat.routine_capacity, chat.knowledge_writable), ((), 20_000, True))
        self.assertEqual((run.outcome.reply, len(routine.batches)), ("Done.", 1))

    def test_a_turn_that_records_nothing_never_sees_the_routines(self) -> None:
        brain = Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, brain)
            controller.chat_turn_service._run_chat_segment(self.request(None))
        (chat,) = brain.contexts
        self.assertEqual((chat.routines, chat.routine_capacity), (None, None))

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
            routine_proposal.chat_routines(subject, "team_1")
        self.assertEqual(caught.exception.code, "routine-state-unavailable")


class RecordingTests(LocalContractCase):
    """A recorded turn keeps each successful call and protects what each attempt was given and returned."""

    def controller(self, directory: str, invoke):
        controller = self._chat_controller(directory, ScriptedRuntime(acting(), completed()))
        controller.assistant_lifecycle.invoke = invoke
        books = controller.chat_turn_service.routine_recordings
        recording = books.start(
            "team_1", (PRINCIPAL, INCARNATION), routine_recorder.Started("List my zones", (), None), int(time.time())
        )
        return controller, books, recording

    def test_a_successful_call_is_an_occurrence_and_its_capabilities_are_protected(self) -> None:
        seen: list[object] = []

        def invoke(_team, _assistant, _action, _payload, evidence):
            seen.append(evidence)
            # The workload's capabilities reach the turn's protection before the RPC.
            evidence.protect(("capability-value-1",))
            return {"result": LOOKUP_RESULT}

        with tempfile.TemporaryDirectory() as directory:
            controller, books, recording = self.controller(directory, invoke)
            run_chat(controller.chat_turn_service, recording)
            found = books.get("team_1", recording)
        (occurrence,) = found.sends[-1].occurrences
        self.assertEqual(
            (occurrence.assistant, occurrence.action, occurrence.read_only, occurrence.operation_id),
            ("shimpz-cloudflare", LIST.action, True, seen[0].operation_id),
        )
        self.assertEqual((occurrence.input.value, occurrence.result.value), (LOOKUP_INPUT, LOOKUP_RESULT))
        self.assertIn("capability-value-1", found.protection.values)
        self.assertRegex(occurrence.pin, r"\Asha256:[0-9a-f]{64}\Z")

    def test_a_failed_call_is_never_an_occurrence_but_keeps_the_protection_it_grew(self) -> None:
        def invoke(_team, _assistant, _action, _payload, evidence):
            evidence.protect(("capability-value-2",))
            raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-action-failed")

        with tempfile.TemporaryDirectory() as directory:
            controller, books, recording = self.controller(directory, invoke)
            with self.assertRaises(local_app.ApiProblem):
                run_chat(controller.chat_turn_service, recording)
            found = books.get("team_1", recording)
        self.assertEqual(found.sends[-1].occurrences, ())
        self.assertIn("capability-value-2", found.protection.values)

    def test_an_ordinary_turn_protects_nothing_and_records_nothing(self) -> None:
        seen: list[object] = []

        def invoke(_team, _assistant, _action, _payload, evidence):
            seen.append(evidence.protect)
            return {"result": LOOKUP_RESULT}

        with tempfile.TemporaryDirectory() as directory:
            controller, books, recording = self.controller(directory, invoke)
            run_chat(controller.chat_turn_service, None)
            found = books.get("team_1", recording)
        self.assertEqual((seen, found.sends[-1].occurrences), ([None], ()))
