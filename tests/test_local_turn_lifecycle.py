from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase, TestPublicationRegistry

from action import execution as action_execution
from action import human as action_human
from inference import client as brain_runtime_client
from local import app as local_app

LOOKUP_INPUT = {"page": 1, "per_page": 25}
LOOKUP_RESULT = {
    "zones": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
TEST_ACCOUNT_ACCESS_TOKEN = "-".join(("oauth", "access", "test", "token", "123456789"))
TEST_ACCOUNT_REFRESH_TOKEN = "-".join(("oauth", "refresh", "test", "token", "123456789"))
CURRENT_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "b" * 64
OUTDATED_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "a" * 64
LOCAL_TEAM_RESIDUES = [
    "action_checkpoints",
    "assistant_containers",
    "brain_checkpoints",
    "chat_continuations",
    "egress_policies",
    "inference_configuration",
    "integration_credentials",
    "publication_bindings",
    "runtime_state",
    "team_networks",
    "team_storage",
]


class LocalTurnLifecycleTests(LocalContractCase):
    @staticmethod
    def _approval_request() -> action_human.HumanRequest:
        descriptor = {
            "kind": "approval",
            "ordinal": 0,
            "title": "List zones",
            "description": "Allow this Action to list the reviewed Cloudflare zones.",
        }
        descriptor["fingerprint"] = action_human._fingerprint(descriptor)
        return action_human.validate_request(descriptor, ("approval",))

    def test_local_human_approval_replays_the_same_action_before_brain_resume(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            resumes = 0

            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, results):
                self.resumes += 1
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("approved result changed")
                return brain_runtime_client.RuntimeTurn("completed", "Approved", ())

        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime()
            controller = self._chat_controller(directory, runtime)
            admitted = self._approval_request()
            invocations: list[tuple[object, ...]] = []

            def invoke(*args):
                invocations.append(args)
                if len(invocations) == 1:
                    raise action_human.HumanRequestSuspensionError(admitted)
                self.assertEqual(args[4], (action_human.admit_response(admitted, True).payload(),))
                return {"result": LOOKUP_RESULT}

            controller.assistant_lifecycle.invoke = invoke
            paused = controller.chat_turn_service.chat(
                "team_1",
                {"message": "List zones", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            self.assertEqual(paused["status"], "human-required")
            self.assertEqual(runtime.resumes, 0)

            completed = controller.chat_turn_service.resume_chat_human(
                "team_1",
                {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True},
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(completed["reply"], "Approved")
        self.assertEqual(runtime.resumes, 1)
        self.assertEqual(len(invocations), 2)

    def test_denied_human_request_purges_the_action_batch_without_brain_resume(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("a denied Action must not resume the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(self._approval_request())
            )
            paused = controller.chat_turn_service.chat(
                "team_1",
                {"message": "List zones", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            denied = controller.chat_turn_service.resume_chat_human(
                "team_1",
                {"challenge_id": paused["challenge_id"], "decision": "deny"},
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                batches = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

        self.assertEqual(denied["status"], "human-denied")
        self.assertEqual(batches, (0,))

    def test_restart_purges_an_expired_human_continuation_and_unblocks_the_generation(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("an expired Action must not resume the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            admitted = self._approval_request()
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
            paused = controller.chat_turn_service.chat(
                "team_1",
                {"message": "List zones", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                before = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

            reopened = local_app.local_chat_continuation_store.EncryptedContinuationStore(
                controller.chat_continuations.state_path,
                controller.chat_continuations.key_path,
                now=lambda: 2_200_000_000,
            )
            restarted = local_app.ChatTurnService(
                local_app.ChatTurnDependencies(
                    action_state=controller.action_state,
                    integration_challenges=local_app.integration_challenges.IntegrationChallengeStore(),
                    human_challenges=local_app.action_challenges.HumanChallengeStore(),
                    chat_continuations=reopened,
                )
            )

            restarted._restore_all_chat_continuations()

            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                after = connection.execute("SELECT COUNT(*) FROM batches").fetchone()
            next_batch = controller.action_state.prepare_batch(
                "a" * 64,
                "next-thread",
                (local_app.action_journal.Operation("action-2", "b" * 64),),
            )

        self.assertEqual(paused["status"], "human-required")
        self.assertEqual(before, (1,))
        self.assertEqual(after, (0,))
        self.assertIsNone(reopened.current("team_1"))
        self.assertEqual(next_batch.generation, "a" * 64)

    def test_running_controller_purges_an_expired_human_challenge(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("an expired Action must not resume the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            admitted = self._approval_request()
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
            controller.chat_turn_service.chat(
                "team_1",
                {"message": "List zones", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            challenge = controller.chat_turn_service.human_challenges.current("team_1")
            self.assertIsNotNone(challenge)

            controller.chat_turn_service.human_challenges._clock = lambda: challenge.expires_at
            controller.chat_turn_service._expire_human_challenges()

            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                batches = connection.execute("SELECT COUNT(*) FROM batches").fetchone()
            next_batch = controller.action_state.prepare_batch(
                "a" * 64,
                "next-thread",
                (local_app.action_journal.Operation("action-2", "b" * 64),),
            )

        self.assertEqual(batches, (0,))
        self.assertIsNone(controller.chat_continuations.current("team_1"))
        self.assertEqual(next_batch.generation, "a" * 64)

    def test_unavailable_strong_local_auth_assurance_auto_blocks_without_a_fake_prompt(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("unavailable authentication must stop the turn")

        for kind in sorted(action_human.AUTH_KINDS - {"auth:reauth"}):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                descriptor = {
                    "kind": kind,
                    "ordinal": 0,
                    "title": "Confirm identity",
                    "description": "Confirm current identity before continuing.",
                }
                descriptor["fingerprint"] = action_human._fingerprint(descriptor)
                admitted = action_human.validate_request(descriptor, (kind,))
                controller = self._chat_controller(directory, Runtime())
                controller.assistant_lifecycle.invoke = lambda *_args, request=admitted: (_ for _ in ()).throw(
                    action_human.HumanRequestSuspensionError(request)
                )

                response = controller.chat_turn_service.chat(
                    "team_1",
                    {
                        "message": "List zones",
                        "files": [],
                        "assistant_ids": ["shimpz-cloudflare"],
                    },
                    "openai",
                    "sk-test-0123456789",
                )
                with closing(sqlite3.connect(controller.action_state.path)) as connection:
                    batches = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

                self.assertEqual(response["status"], "human-denied")
                self.assertEqual(response["reason"], "authentication-unavailable")
                self.assertEqual(batches, (0,))
                self.assertIsNone(controller.chat_turn_service.human_challenges.current("team_1"))
                self.assertIsNone(controller.chat_turn_service.chat_continuations.current("team_1"))

    def test_local_reauthentication_pauses_for_supervisor_assurance(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("reauthentication must pause before Action replay")

        descriptor = {
            "kind": "auth:reauth",
            "ordinal": 0,
            "title": "Confirm identity",
            "description": "Confirm current identity before continuing.",
        }
        descriptor["fingerprint"] = action_human._fingerprint(descriptor)
        admitted = action_human.validate_request(descriptor, ("auth:reauth",))

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
            response = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                },
                "openai",
                "sk-test-0123456789",
            )

            self.assertEqual(response["status"], "human-required")
            self.assertEqual(response["request"]["kind"], "auth:reauth")
            self.assertIsNotNone(controller.chat_turn_service.human_challenges.current("team_1"))
            self.assertIsNotNone(controller.chat_turn_service.chat_continuations.current("team_1"))

    def test_failed_reauthentication_resume_requires_a_fresh_request_without_wedging_team(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, results):
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("reauthenticated result changed")
                return brain_runtime_client.RuntimeTurn("completed", "Recovered", ())

        descriptor = {
            "kind": "auth:reauth",
            "ordinal": 0,
            "title": "Confirm identity",
            "description": "Confirm current identity before continuing.",
        }
        descriptor["fingerprint"] = action_human._fingerprint(descriptor)
        admitted = action_human.validate_request(descriptor, ("auth:reauth",))

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invocations: list[tuple[object, ...]] = []

            def invoke(*args):
                invocations.append(args)
                if len(invocations) in {1, 3}:
                    raise action_human.HumanRequestSuspensionError(admitted)
                if len(invocations) == 2:
                    raise local_app.ApiProblem(
                        HTTPStatus.BAD_GATEWAY,
                        "private Assistant failure",
                        code="assistant-rpc-failed",
                    )
                return {"result": LOOKUP_RESULT}

            controller.assistant_lifecycle.invoke = invoke
            first_pause = controller.chat_turn_service.chat(
                "team_1",
                {"message": "List zones", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            with self.assertRaises(local_app.ApiProblem) as failed:
                controller.chat_turn_service.resume_chat_human(
                    "team_1",
                    {"challenge_id": first_pause["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    "sk-test-0123456789",
                )
            second_pause = controller.chat_turn_service.chat(
                "team_1",
                {"message": "List zones", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            completed = controller.chat_turn_service.resume_chat_human(
                "team_1",
                {"challenge_id": second_pause["challenge_id"], "decision": "submit", "value": True},
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(failed.exception.code, "assistant-rpc-failed")
        self.assertEqual(first_pause["status"], "human-required")
        self.assertEqual(second_pause["status"], "human-required")
        self.assertNotEqual(first_pause["challenge_id"], second_pause["challenge_id"])
        self.assertEqual(completed["reply"], "Recovered")
        self.assertEqual(len(invocations), 4)

    def test_chat_stop_does_not_hold_the_global_guard_during_action_termination(self) -> None:
        token = "turn-token"
        container = object()
        stop_started = threading.Event()
        release_stop = threading.Event()
        result: list[dict[str, object]] = []
        service = local_app.ChatTurnService(
            local_app.ChatTurnDependencies(
                integration_challenges=SimpleNamespace(cancel_team=lambda _team_id: False),
                oauth_pkce=SimpleNamespace(cancel_team=lambda _team_id: None),
            )
        )
        service._delete_chat_continuation = lambda _team_id: False
        service._active_chat_tokens["team_1"] = token
        service._active_action_containers["team_1"] = (token, container)

        def fail_stop_action(actual_container: object) -> None:
            self.assertIs(actual_container, container)
            self.assertIn(token, service._cancelled_chat_tokens)
            stop_started.set()
            release_stop.wait(timeout=2)

        service.assistant_lifecycle = SimpleNamespace(
            _network=lambda _team_id: None,
            _fail_stop_action=fail_stop_action,
        )
        worker = threading.Thread(target=lambda: result.append(service.stop_chat("team_1")), daemon=True)
        worker.start()
        self.assertTrue(stop_started.wait(timeout=1))
        self.assertTrue(service._active_chat_guard.acquire(timeout=0.1))
        service._active_chat_guard.release()
        release_stop.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertIs(result[0]["confirmed"], True)

    def test_destroy_drains_chat_and_deletes_generation_before_teardown(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
        controller.assistant_integrations = SimpleNamespace(
            delete_team=lambda team_id: events.append(("integrations-delete", team_id))
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
        controller.registry = TestPublicationRegistry({"shimpz-cloudflare": SimpleNamespace(allowed_hosts=())})
        controller.client = SimpleNamespace(containers=SimpleNamespace(list=list_containers))
        controller.brain_runtime = SimpleNamespace(
            delete_thread=lambda thread_id: events.append(("thread-delete", thread_id))
        )
        controller.action_state = SimpleNamespace(purge=lambda generation: events.append(("action-purge", generation)))
        controller.storage = SimpleNamespace(destroy=lambda _team_id: events.append("storage-destroy") or True)
        controller.inference_store = SimpleNamespace(delete=lambda _team_id: events.append("inference-delete"))
        controller._wire_collaborators()
        controller.chat_turn_service._active_chat_tokens = {"team_1": "turn-token"}
        controller.chat_turn_service._active_action_containers = {"team_1": ("turn-token", object())}
        controller.chat_turn_service._chat_lock = lambda _team_id: ChatLock()
        controller.assistant_lifecycle._fail_stop_action = lambda _container: events.append("action-stopped")
        controller.assistant_lifecycle._network = lambda _team_id, *, required=False: (
            events.append("network-read") or network
        )
        controller.assistant_lifecycle._assistant_filters = lambda _team_id: {}
        controller.assistant_lifecycle._validate_container_profile = lambda *_args: events.append("container-validated")
        controller.assistant_lifecycle._queue_residue = lambda image_id: events.append(("residue-add", image_id))
        controller.assistant_lifecycle.sweep_residues = lambda: events.append("residue-sweep")

        result = controller.destroy_team("team_1")

        expected_thread = local_app._brain_thread_id("local-space", "team_1", "a" * 64)
        self.assertEqual(
            events,
            [
                "action-stopped",
                ("chat-lock", 30),
                "lifecycle-lock",
                "network-read",
                "containers-read",
                "container-validated",
                ("thread-delete", expected_thread),
                ("action-purge", "a" * 64),
                ("container-remove", True),
                ("residue-add", "sha256:" + "a" * 64),
                "residue-sweep",
                "storage-destroy",
                "inference-delete",
                "network-remove",
                ("integrations-delete", "team_1"),
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

    def test_reset_removes_orphan_egress_authority_for_owned_teams(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(clear=lambda: 0)
        controller._locks = (threading.RLock(),)
        controller.registry = TestPublicationRegistry({"shimpz-cloudflare": SimpleNamespace()})
        network = SimpleNamespace(
            id="a" * 64,
            attrs={"Labels": {local_app.TEAM_LABEL: "team_1"}},
            remove=lambda: events.append("network-remove"),
        )
        controller.client = SimpleNamespace(
            containers=SimpleNamespace(list=lambda **_kwargs: []),
            networks=SimpleNamespace(list=lambda **_kwargs: [network]),
        )
        controller.storage = SimpleNamespace(destroy_all=lambda: events.append("destroy-storage") or True)
        controller.inference_store = SimpleNamespace(
            delete=lambda team_id: events.append(("delete-inference", team_id))
        )
        controller.brain_runtime = SimpleNamespace(
            delete_thread=lambda thread_id: events.append(("delete-thread", thread_id))
        )
        controller.action_state = SimpleNamespace(purge=lambda generation: events.append(("purge-action", generation)))
        controller._wire_collaborators()
        controller.assistant_lifecycle._validate_network = lambda _network, team_id, **_kwargs: events.append(
            ("validate-network", team_id)
        )
        controller.chat_turn_service._delete_all_integration_state = lambda: events.append("delete-integrations")
        controller.assistant_lifecycle._remove_egress_policy = lambda team_id, assistant_id: events.append(
            ("remove-policy", team_id, assistant_id)
        )
        controller.assistant_lifecycle._disconnect_egress_proxy_if_attached = lambda _network: events.append(
            "disconnect-proxy"
        )
        controller.assistant_lifecycle.sweep_residues = lambda: events.append("residue-sweep")
        result = controller.reset_space()

        self.assertEqual(result["assistants_removed"], 0)
        self.assertEqual(result["teams_removed"], 1)
        self.assertEqual(
            result["residue_absent"],
            LOCAL_TEAM_RESIDUES,
        )
        self.assertIn(("remove-policy", "team_1", "shimpz-cloudflare"), events)
        self.assertIn(("purge-action", "a" * 64), events)
        self.assertIn("residue-sweep", events)
        self.assertEqual(controller.registry.identities(), set())
        self.assertLess(events.index("delete-integrations"), events.index("network-remove"))

    def test_reset_queues_removed_assistant_images_before_the_final_sweep(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(clear=lambda: 0)
        controller._locks = (threading.RLock(),)
        spec = SimpleNamespace(
            assistant_id="shimpz-cloudflare",
            image=CURRENT_ASSISTANT_IMAGE,
            allowed_hosts=(),
        )
        controller.registry = TestPublicationRegistry({spec.assistant_id: spec})
        controller.client = SimpleNamespace(
            containers=SimpleNamespace(list=lambda **_kwargs: []),
            networks=SimpleNamespace(list=lambda **_kwargs: []),
        )
        controller.storage = SimpleNamespace(destroy_all=lambda: events.append("destroy-storage") or True)
        controller.inference_store = SimpleNamespace(delete=lambda _team_id: None)
        controller.brain_runtime = SimpleNamespace(delete_thread=lambda _thread_id: None)
        controller.action_state = SimpleNamespace(purge=lambda _generation: None)
        controller._wire_collaborators()
        labels = controller.assistant_lifecycle._assistant_labels("team_1", spec)
        container = SimpleNamespace(
            id="assistant-container",
            name=controller.assistant_lifecycle._container_name("team_1", spec.assistant_id),
            attrs={"Image": "sha256:" + "a" * 64, "Config": {"Labels": labels}},
            reload=lambda: events.append("container-reload"),
            remove=lambda *, force: events.append(("container-remove", force)),
        )
        controller.client.containers.list = lambda **_kwargs: [container]
        controller.chat_turn_service._delete_all_integration_state = lambda: events.append("delete-integrations")
        controller.assistant_lifecycle._remove_egress_policy = lambda team_id, assistant_id: events.append(
            ("remove-policy", team_id, assistant_id)
        )
        controller.assistant_lifecycle._queue_residue = lambda image_id: events.append(("residue-add", image_id))
        controller.assistant_lifecycle.sweep_residues = lambda: events.append("residue-sweep")
        controller._clear_team_runtime_state = lambda team_id: events.append(("clear-runtime", team_id))

        result = controller.reset_space()

        self.assertEqual((result["assistants_removed"], result["teams_removed"]), (1, 0))
        self.assertLess(events.index(("container-remove", True)), events.index(("residue-add", "sha256:" + "a" * 64)))
        self.assertLess(
            events.index(("remove-policy", "team_1", "shimpz-cloudflare")),
            events.index("residue-sweep"),
        )

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
        controller.registry = TestPublicationRegistry({"shimpz-cloudflare": SimpleNamespace(allowed_hosts=())})
        controller.client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_filters: [container]))

        def fail_delete(_thread_id: str) -> None:
            raise brain_runtime_client.BrainRuntimeError("private-checkpoint-data")

        controller.brain_runtime = SimpleNamespace(delete_thread=fail_delete)
        controller.action_state = SimpleNamespace(
            purge=lambda _generation: self.fail("journal purge ran after Brain deletion failed")
        )
        controller.storage = SimpleNamespace(destroy=lambda _team_id: events.append("storage-destroy"))
        controller.inference_store = SimpleNamespace(delete=lambda _team_id: events.append("inference-delete"))
        controller._wire_collaborators()
        controller.chat_turn_service._chat_lock = lambda _team_id: lock
        controller.assistant_lifecycle._network = lambda _team_id, *, required=False: network
        controller.assistant_lifecycle._assistant_filters = lambda _team_id: {}
        controller.assistant_lifecycle._validate_container_profile = lambda *_args: None

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.destroy_team("team_1")

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
        controller.registry = TestPublicationRegistry({"shimpz-cloudflare": SimpleNamespace(allowed_hosts=())})
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
        controller._wire_collaborators()
        controller.chat_turn_service._chat_lock = lambda _team_id: lock
        controller.assistant_lifecycle._network = lambda _team_id, *, required=False: network
        controller.assistant_lifecycle._assistant_filters = lambda _team_id: {}
        controller.assistant_lifecycle._validate_container_profile = lambda *_args: None

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.destroy_team("team_1")

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

    def test_team_identity_drift_stops_before_the_provider_call(self) -> None:
        class Runtime:
            def start(self, _context, _message):
                raise AssertionError("a changed Team must not reach the provider")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            names = iter(("Marketing", "Renamed"))
            controller.assistant_lifecycle._validate_network = lambda _network, _team_id, **_kwargs: next(names)

            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.chat_turn_service.chat(
                    "team_1",
                    {"message": "Hello", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                    "openai",
                    "sk-test-0123456789",
                )

        self.assertEqual(caught.exception.code, "team-context-changed")

    def test_chat_executes_only_a_controller_owned_declared_action(self) -> None:
        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn(
                    status="action-required",
                    reply="",
                    actions=(
                        brain_runtime_client.ActionRequest(
                            interrupt_id="action-1",
                            assistant_id="shimpz-cloudflare",
                            action="list-zones",
                            input=LOOKUP_INPUT,
                        ),
                    ),
                )

            def resume(self, _context, results):
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("Action result did not return through the Controller")
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Done", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invoked: list[tuple[str, str, object]] = []
            controller.invoke = lambda team_id, assistant, action, payload: (
                invoked.append((team_id, assistant, payload))
                or {"assistant": assistant, "action": action, "result": LOOKUP_RESULT}
            )
            controller.assistant_lifecycle.invoke = controller.invoke
            response = controller.chat_turn_service.chat(
                "team_1",
                {"message": "Greet me", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(invoked, [("team_1", "shimpz-cloudflare", LOOKUP_INPUT)])
        self.assertEqual(response, {"team_id": "team_1", "team_name": "Marketing", "reply": "Done"})

    def test_chat_reuses_a_completed_action_after_resume_failure_then_delivers(self) -> None:
        request = brain_runtime_client.ActionRequest(
            interrupt_id="action-1",
            assistant_id="shimpz-cloudflare",
            action="list-zones",
            input=LOOKUP_INPUT,
        )

        class Runtime:
            resumes = 0

            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn(status="action-required", reply="", actions=(request,))

            def resume(self, _context, results):
                self.resumes += 1
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("cached Action result changed")
                if self.resumes == 1:
                    raise brain_runtime_client.BrainRuntimeError("private-resume-failure")
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Done", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invocations: list[object] = []
            controller.invoke = lambda _team_id, assistant, action, payload: (
                invocations.append(payload) or {"assistant": assistant, "action": action, "result": LOOKUP_RESULT}
            )
            controller.assistant_lifecycle.invoke = controller.invoke
            with self.assertRaises(local_app.ApiProblem) as first:
                controller.chat_turn_service.chat(
                    "team_1",
                    {"message": "Greet me", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                    "openai",
                    "sk-test-0123456789",
                )

            response = controller.chat_turn_service.chat(
                "team_1",
                {"message": "Greet me", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                pending = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

        self.assertEqual(first.exception.code, "brain-runtime-failed")
        self.assertNotIn("private-resume-failure", str(first.exception))
        self.assertEqual(invocations, [LOOKUP_INPUT])
        self.assertEqual(response["reply"], "Done")
        self.assertEqual(pending, (0,))

    def test_chat_purges_an_orphaned_paused_batch_by_network_generation(self) -> None:
        class Runtime:
            @staticmethod
            def start(_context, _message):
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Recovered", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            first = local_app.action_journal.Operation("action-1", "b" * 64)
            second = local_app.action_journal.Operation("action-2", "c" * 64)
            orphan = controller.action_state.prepare_batch("a" * 64, "orphan-thread", (first, second))
            controller.action_state.begin(orphan, first)
            controller.action_state.complete(orphan, first, {"ok": True})
            controller.action_state.begin(orphan, second)
            controller.action_state.suspend(orphan, second)

            response = controller.chat_turn_service.chat(
                "team_1",
                {"message": "Start a fresh turn", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                batches = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

        self.assertEqual(response["reply"], "Recovered")
        self.assertEqual(batches, (0,))

    def test_terminal_rpc_failure_does_not_wedge_the_next_independent_turn(self) -> None:
        request = brain_runtime_client.ActionRequest(
            interrupt_id="action-1",
            assistant_id="shimpz-cloudflare",
            action="list-zones",
            input=LOOKUP_INPUT,
        )

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn(status="action-required", reply="", actions=(request,))

            def resume(self, _context, results):
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("the independent Action result changed")
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Recovered", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invocations: list[object] = []

            def fail_rpc(*_args):
                invocations.append("rpc")
                if len(invocations) == 1:
                    raise local_app.ApiProblem(
                        HTTPStatus.BAD_GATEWAY,
                        "private Assistant failure",
                        code="assistant-rpc-failed",
                    )
                return {"result": LOOKUP_RESULT}

            controller.invoke = fail_rpc
            controller.assistant_lifecycle.invoke = controller.invoke
            with self.assertRaises(local_app.ApiProblem) as first:
                controller.chat_turn_service.chat(
                    "team_1",
                    {"message": "Greet me", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                    "openai",
                    "sk-test-0123456789",
                )
            self.assertEqual(invocations, ["rpc"])
            retry = controller.chat_turn_service.chat(
                "team_1",
                {"message": "Greet me", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(first.exception.code, "assistant-rpc-failed")
        self.assertNotIn("private Assistant failure", str(retry))
        self.assertEqual(retry["reply"], "Recovered")
        self.assertEqual(invocations, ["rpc", "rpc"])

    def test_crash_uncertain_batch_remains_blocked_across_identical_local_retries(self) -> None:
        request = brain_runtime_client.ActionRequest(
            interrupt_id="action-1",
            assistant_id="shimpz-cloudflare",
            action="list-zones",
            input=LOOKUP_INPUT,
        )

        class Runtime:
            @staticmethod
            def start(_context, _message):
                return brain_runtime_client.RuntimeTurn(status="action-required", reply="", actions=(request,))

            @staticmethod
            def resume(_context, _results):
                raise AssertionError("an uncertain Action must not reach Brain resume")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            assistant = controller.registry["shimpz-cloudflare"]
            operation = action_execution.action_operation(
                request,
                "assistant-container",
                assistant.image,
                integration_generations=(("cloudflare", 1),),
            )
            generation = "a" * 64
            batch = controller.action_state.prepare_batch(
                generation,
                local_app._brain_thread_id("local-space", "team_1", generation),
                (operation,),
            )
            controller.action_state.begin(batch, operation)
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                AssertionError("an uncertain Action must not execute")
            )

            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaises(local_app.ApiProblem) as failed:
                    controller.chat_turn_service.chat(
                        "team_1",
                        {"message": "Greet me", "files": [], "assistant_ids": ["shimpz-cloudflare"]},
                        "openai",
                        "sk-test-0123456789",
                    )
                self.assertEqual(failed.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(failed.exception.code, "action-state-unavailable")

            with self.assertRaises(local_app.action_journal.ActionJournalUncertainError):
                controller.action_state.begin(batch, operation)
