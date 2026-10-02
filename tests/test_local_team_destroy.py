"""Destroying a Local Team drains its chat and removes its owned state before teardown, failing closed."""

from __future__ import annotations

import contextlib
import threading
from http import HTTPStatus
from types import SimpleNamespace

from local_controller_harness import LocalContractCase, TestAssistantRegistry
from test_local_turn_lifecycle import LOCAL_TEAM_RESIDUES

from action import challenges as action_challenges
from inference import client as brain_runtime_client
from local import app as local_app
from routine import record as routine_record


class LocalTeamDestroyTests(LocalContractCase):
    def test_destroy_drains_chat_and_deletes_generation_before_teardown(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
        controller.assistant_integrations = SimpleNamespace(
            delete_team=lambda team_id: events.append(("integrations-delete", team_id))
        )
        controller.assistant_stored_inputs = SimpleNamespace(
            delete_team=lambda team_id: events.append(("stored-inputs-delete", team_id))
        )

        class ChatLock:
            def acquire(self, *, timeout: int) -> bool:
                events.append(("chat-lock", timeout))
                return True

            def release(self) -> None:
                events.append("chat-release")

        class LifecycleLock:
            def __enter__(self):
                events.append("lifecycle-lock")

            def __exit__(self, *_args) -> None:
                events.append("lifecycle-release")

        network = SimpleNamespace(
            id="a" * 64,
            name="team-network",
            attrs={"Containers": {}},
            reload=lambda: None,
            remove=lambda: events.append("network-remove"),
        )
        container = SimpleNamespace(
            id="assistant-container",
            labels={local_app.ASSISTANT_LABEL: "shimpz-cloudflare"},
            attrs={"Image": "sha256:" + "a" * 64},
            remove=lambda *, force: events.append(("container-remove", force)),
        )

        def list_containers(**_filters):
            events.append("containers-read")
            return [container]

        controller._lock = lambda _team_id: LifecycleLock()
        controller._names_lock = threading.RLock()
        controller.registry = TestAssistantRegistry(
            {
                "shimpz-cloudflare": SimpleNamespace(
                    allowed_hosts=(),
                    provenance="published",
                )
            }
        )
        controller.client = SimpleNamespace(containers=SimpleNamespace(list=list_containers))
        controller.brain_runtime = SimpleNamespace(
            delete_thread=lambda thread_id: events.append(("thread-delete", thread_id))
        )
        controller.action_state = SimpleNamespace(purge=lambda generation: events.append(("action-purge", generation)))
        controller.storage = SimpleNamespace(destroy=lambda _team_id: events.append("storage-destroy") or True)
        controller.inference_store = SimpleNamespace(delete=lambda _team_id: events.append("inference-delete"))
        controller.routine_proposals = SimpleNamespace(drop_team=lambda _team_id: None, fenced=contextlib.nullcontext)
        controller.routine_human_challenges = action_challenges.HumanChallengeStore()
        controller.routine_store = SimpleNamespace(
            load=lambda _team_id: routine_record.TeamRoutines(),
            delete=lambda _team_id: events.append("routines-delete"),
            teams=lambda: (),
            delete_all=lambda: events.append("routines-delete-all"),
            lock=lambda _team_id: contextlib.nullcontext(),
            exclusive=contextlib.nullcontext,
        )
        controller.routine_diagnostics = SimpleNamespace(
            delete=lambda _team_id: events.append("diagnostics-delete"),
            delete_all=lambda: events.append("diagnostics-delete-all"),
            delete_routine=lambda _team_id, _routine_id: None,
        )
        controller._wire_collaborators()
        controller.chat_turn_service._active_chat_tokens = {"team_1": "turn-token"}
        controller.chat_turn_service._active_action_containers = {"team_1": ("turn-token", object())}
        controller.chat_turn_service._chat_lock = lambda _team_id: ChatLock()
        controller.assistant_lifecycle._fail_stop_action = lambda _container: events.append("action-stopped")
        controller.assistant_lifecycle._network = lambda _team_id, *, required=False: (
            events.append("network-read") or network
        )
        controller.assistant_lifecycle._assistant_filters = lambda _team_id: {}
        controller.assistant_lifecycle._validate_network = lambda *_args, **_kwargs: "Team One"
        controller.team_names = SimpleNamespace(
            load=lambda _team_id, _network_id: None, delete=lambda _team_id: events.append("names-delete")
        )
        controller.assistant_lifecycle._validate_container_profile = lambda *_args: events.append("container-validated")
        controller.assistant_lifecycle._queue_residue = lambda image_id: events.append(("residue-add", image_id))
        controller.assistant_lifecycle.sweep_residues = lambda: events.append("residue-sweep")

        result = controller.destroy_team("team_1", "Team One")

        expected_thread = local_app._brain_thread_id("local-space", "team_1", "a" * 64)
        self.assertEqual(
            events,
            [
                # The name is confirmed under the Team lock before any side effect (ADR-0088).
                "lifecycle-lock",
                "network-read",
                "lifecycle-release",
                "action-stopped",
                ("chat-lock", 30),
                "lifecycle-lock",
                "network-read",
                "containers-read",
                "container-validated",
                ("thread-delete", expected_thread),
                ("action-purge", "a" * 64),
                "routines-delete",
                "diagnostics-delete",
                ("container-remove", True),
                ("residue-add", "sha256:" + "a" * 64),
                "residue-sweep",
                "storage-destroy",
                "inference-delete",
                "network-remove",
                "names-delete",
                ("integrations-delete", "team_1"),
                ("stored-inputs-delete", "team_1"),
                "lifecycle-release",
                "chat-release",
            ],
        )
        self.assertEqual(
            result,
            {
                "team_id": "team_1",
                "destroyed": True,
                "assistants_removed": 1,
                "storage_removed": True,
                "residue_absent": LOCAL_TEAM_RESIDUES,
            },
        )
        self.assertEqual(controller.registry.identities(), set())

    def test_destroy_brain_failure_is_redacted_and_mutates_nothing(self) -> None:
        events: list[str] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
        lock = threading.Lock()
        network = SimpleNamespace(
            id="a" * 64,
            name="team-network",
            remove=lambda: events.append("network-remove"),
        )
        container = SimpleNamespace(
            id="assistant-container",
            labels={local_app.ASSISTANT_LABEL: "shimpz-cloudflare"},
            remove=lambda *, force: events.append("container-remove"),
        )
        controller._lock = lambda _team_id: threading.RLock()
        controller._names_lock = threading.RLock()
        controller.registry = TestAssistantRegistry({"shimpz-cloudflare": SimpleNamespace(allowed_hosts=())})
        controller.client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_filters: [container]))

        def fail_delete(_thread_id: str) -> None:
            raise brain_runtime_client.BrainRuntimeError("private-checkpoint-data")

        controller.brain_runtime = SimpleNamespace(delete_thread=fail_delete)
        controller.action_state = SimpleNamespace(
            purge=lambda _generation: self.fail("journal purge ran after Brain deletion failed")
        )
        controller.storage = SimpleNamespace(destroy=lambda _team_id: events.append("storage-destroy"))
        controller.inference_store = SimpleNamespace(delete=lambda _team_id: events.append("inference-delete"))
        controller.routine_proposals = SimpleNamespace(drop_team=lambda _team_id: None, fenced=contextlib.nullcontext)
        controller.routine_human_challenges = action_challenges.HumanChallengeStore()
        controller.routine_store = SimpleNamespace(
            load=lambda _team_id: routine_record.TeamRoutines(),
            delete=lambda _team_id: events.append("routines-delete"),
            teams=lambda: (),
            delete_all=lambda: events.append("routines-delete-all"),
            lock=lambda _team_id: contextlib.nullcontext(),
            exclusive=contextlib.nullcontext,
        )
        controller.routine_diagnostics = SimpleNamespace(
            delete=lambda _team_id: events.append("diagnostics-delete"),
            delete_all=lambda: events.append("diagnostics-delete-all"),
            delete_routine=lambda _team_id, _routine_id: None,
        )
        controller._wire_collaborators()
        controller.chat_turn_service._chat_lock = lambda _team_id: lock
        controller.assistant_lifecycle._network = lambda _team_id, *, required=False: network
        controller.assistant_lifecycle._assistant_filters = lambda _team_id: {}
        controller.assistant_lifecycle._validate_network = lambda *_args, **_kwargs: "Team One"
        controller.team_names = SimpleNamespace(
            load=lambda _team_id, _network_id: None, delete=lambda _team_id: events.append("names-delete")
        )
        controller.assistant_lifecycle._validate_container_profile = lambda *_args: None

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.destroy_team("team_1", "Team One")

        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(caught.exception.message, "Team conversation state could not be deleted")
        self.assertNotIn("private-checkpoint-data", str(caught.exception))
        self.assertEqual(events, [])
        self.assertFalse(lock.locked())

    def test_destroy_journal_failure_is_redacted_before_teardown(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
        lock = threading.Lock()
        network = SimpleNamespace(
            id="a" * 64,
            name="team-network",
            remove=lambda: events.append("network-remove"),
        )
        container = SimpleNamespace(
            id="assistant-container",
            labels={local_app.ASSISTANT_LABEL: "shimpz-cloudflare"},
            remove=lambda *, force: events.append(("container-remove", force)),
        )
        controller._lock = lambda _team_id: threading.RLock()
        controller._names_lock = threading.RLock()
        controller.registry = TestAssistantRegistry({"shimpz-cloudflare": SimpleNamespace(allowed_hosts=())})
        controller.client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_filters: [container]))
        controller.brain_runtime = SimpleNamespace(
            delete_thread=lambda thread_id: events.append(("thread-delete", thread_id))
        )

        def fail_purge(generation: str) -> None:
            events.append(("action-purge", generation))
            raise local_app.action_journal.ActionJournalError("private-journal-path")

        controller.action_state = SimpleNamespace(purge=fail_purge)
        controller.storage = SimpleNamespace(destroy=lambda _team_id: events.append("storage-destroy"))
        controller.inference_store = SimpleNamespace(delete=lambda _team_id: events.append("inference-delete"))
        controller.routine_proposals = SimpleNamespace(drop_team=lambda _team_id: None, fenced=contextlib.nullcontext)
        controller.routine_human_challenges = action_challenges.HumanChallengeStore()
        controller.routine_store = SimpleNamespace(
            load=lambda _team_id: routine_record.TeamRoutines(),
            delete=lambda _team_id: events.append("routines-delete"),
            teams=lambda: (),
            delete_all=lambda: events.append("routines-delete-all"),
            lock=lambda _team_id: contextlib.nullcontext(),
            exclusive=contextlib.nullcontext,
        )
        controller.routine_diagnostics = SimpleNamespace(
            delete=lambda _team_id: events.append("diagnostics-delete"),
            delete_all=lambda: events.append("diagnostics-delete-all"),
            delete_routine=lambda _team_id, _routine_id: None,
        )
        controller._wire_collaborators()
        controller.chat_turn_service._chat_lock = lambda _team_id: lock
        controller.assistant_lifecycle._network = lambda _team_id, *, required=False: network
        controller.assistant_lifecycle._assistant_filters = lambda _team_id: {}
        controller.assistant_lifecycle._validate_network = lambda *_args, **_kwargs: "Team One"
        controller.team_names = SimpleNamespace(
            load=lambda _team_id, _network_id: None, delete=lambda _team_id: events.append("names-delete")
        )
        controller.assistant_lifecycle._validate_container_profile = lambda *_args: None

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.destroy_team("team_1", "Team One")

        expected_thread = local_app._brain_thread_id("local-space", "team_1", "a" * 64)
        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(caught.exception.code, "action-state-unavailable")
        self.assertEqual(caught.exception.message, "Team Action execution state could not be deleted")
        self.assertNotIn("private-journal-path", str(caught.exception))
        self.assertEqual(
            events,
            [("thread-delete", expected_thread), ("action-purge", "a" * 64)],
        )
        self.assertFalse(lock.locked())
