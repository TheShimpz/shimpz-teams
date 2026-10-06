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
from integrations import challenges as integration_challenges
from integrations import pkce as integration_pkce
from local import app as local_app
from local.chat.types import PendingLocalChat
from local.routine import card as routine_card
from local.routine import lineage as routine_lineage
from local.routine import recent as routine_recent
from routine import record as routine_record


def _expired_human(team_id: str, generation: str) -> action_challenges.PendingHumanChallenge:
    pending = PendingLocalChat(object(), (), (), "openai", ("identity", generation, "", "", ""), paused_batch="f" * 64)
    return action_challenges.PendingHumanChallenge("e" * 32, team_id, 0.0, SimpleNamespace(), pending)


def _routine_books(controller: local_app.LocalController) -> None:
    """The Team's in-memory Routine books a destroy must empty: challenges, questions, recent sends, and cards."""
    controller.routine_human_challenges = action_challenges.HumanChallengeStore()
    controller.routine_lineage = routine_lineage.LineageBook()
    controller.routine_recent = routine_recent.RecentBook()
    controller.routine_cards = routine_card.CardBook()


def _record_routine_state(controller: local_app.LocalController, events: list[object]) -> None:
    """Routine stores that record every deletion a destroy asks of them."""
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


def _destroy_controller(
    events: list[object], brain_runtime: object, action_state: object
) -> tuple[local_app.LocalController, threading.Lock]:
    """Team team_1 ("Team One") with one Assistant, recording teardown; returns it and its chat lock."""
    controller = object.__new__(local_app.LocalController)
    controller.space_id = "local-space"
    controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
    controller.integration_challenges = integration_challenges.IntegrationChallengeStore()
    controller.oauth_pkce = integration_pkce.OAuthPKCEChallengeStore()
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
    controller.brain_runtime = brain_runtime
    controller.action_state = action_state
    controller.storage = SimpleNamespace(destroy=lambda _team_id: events.append("storage-destroy"))
    controller.inference_store = SimpleNamespace(delete=lambda _team_id: events.append("inference-delete"))
    _routine_books(controller)
    _record_routine_state(controller, events)
    controller._wire_collaborators()
    controller.chat_turn_service._chat_lock = lambda _team_id: lock
    controller.assistant_lifecycle._network = lambda _team_id, *, required=False: network
    controller.assistant_lifecycle._assistant_filters = lambda _team_id: {}
    controller.assistant_lifecycle._validate_network = lambda *_args, **_kwargs: "Team One"
    controller.team_names = SimpleNamespace(
        load=lambda _team_id, _network_id: None, delete=lambda _team_id: events.append("names-delete")
    )
    controller.assistant_lifecycle._validate_container_profile = lambda *_args: None
    return controller, lock


class LocalTeamDestroyTests(LocalContractCase):
    def test_destroy_drains_chat_and_deletes_generation_before_teardown(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
        controller.integration_challenges = integration_challenges.IntegrationChallengeStore()
        controller.oauth_pkce = integration_pkce.OAuthPKCEChallengeStore()
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

        def list_containers(**filters):
            if "com.shimpz.local.kind=prepare" in filters.get("filters", {}).get("label", []):
                events.append("helpers-read")
                return []
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
        controller.action_state = SimpleNamespace(
            purge=lambda generation: events.append(("action-purge", generation)),
            purge_batch=lambda generation, batch: events.append(("action-purge-batch", generation, batch)),
        )
        controller.storage = SimpleNamespace(destroy=lambda _team_id: events.append("storage-destroy") or True)
        controller.inference_store = SimpleNamespace(delete=lambda _team_id: events.append("inference-delete"))
        _routine_books(controller)
        _record_routine_state(controller, events)
        controller._wire_collaborators()
        # Human continuations that already expired, one of this Team's earlier generation and one of another Team.
        humans = controller.chat_turn_service.human_challenges
        foreign = _expired_human("team_2", "d" * 64)
        humans._expired.extend((_expired_human("team_1", "c" * 64), foreign))
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
                # The Team's expired continuation is cleaned by its owning consumer, never left behind.
                ("action-purge-batch", "c" * 64, "f" * 64),
                ("thread-delete", expected_thread),
                ("action-purge", "a" * 64),
                "helpers-read",
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
        self.assertEqual(humans.drain_expired(), (foreign,))

    def test_destroy_brain_failure_is_redacted_and_mutates_nothing(self) -> None:
        events: list[object] = []

        def fail_delete(_thread_id: str) -> None:
            raise brain_runtime_client.BrainRuntimeError("private-checkpoint-data")

        controller, lock = _destroy_controller(
            events,
            SimpleNamespace(delete_thread=fail_delete),
            SimpleNamespace(purge=lambda _generation: self.fail("journal purge ran after Brain deletion failed")),
        )

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.destroy_team("team_1", "Team One")

        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(caught.exception.message, "Team conversation state could not be deleted")
        self.assertNotIn("private-checkpoint-data", str(caught.exception))
        self.assertEqual(events, [])
        self.assertFalse(lock.locked())

    def test_destroy_journal_failure_is_redacted_before_teardown(self) -> None:
        events: list[object] = []

        def fail_purge(generation: str) -> None:
            events.append(("action-purge", generation))
            raise local_app.action_journal.ActionJournalError("private-journal-path")

        controller, lock = _destroy_controller(
            events,
            SimpleNamespace(delete_thread=lambda thread_id: events.append(("thread-delete", thread_id))),
            SimpleNamespace(purge=fail_purge),
        )

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.destroy_team("team_1", "Team One")

        expected_thread = local_app._brain_thread_id("local-space", "team_1", "a" * 64)
        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(caught.exception.code, "action-state-unavailable")
        self.assertEqual(caught.exception.message, "Team Action execution state is unavailable")
        self.assertNotIn("private-journal-path", str(caught.exception))
        self.assertEqual(
            events,
            [("thread-delete", expected_thread), ("action-purge", "a" * 64)],
        )
        self.assertFalse(lock.locked())
