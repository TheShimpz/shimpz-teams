from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from local_assistant_fixture import PACK as FIXTURE_PACK
from local_assistant_fixture import assistant_spec

from action import execution as action_execution
from action import stored_input as action_stored_input
from inference import config as inference_config
from integrations import challenges as integration_challenges
from integrations import pkce as integration_pkce
from integrations import store as integration_store
from local import app as local_app
from local import names as local_names
from local.assistant import lifecycle as assistant_lifecycle
from local.chat import continuation_store as local_chat_continuation_store
from local.chat.types import ActiveAssistant
from local.install.runtime import AssistantSpec
from local.routine import diagnostics as local_routine_diagnostics
from local.routine import proposal as local_routine_proposal
from local.routine import protection as local_routine_protection
from local.routine import recorder as local_routine_recorder
from local.routine import store as local_routine_store

TEST_ACCOUNT_ACCESS_TOKEN = "-".join(("oauth", "access", "test", "token", "123456789"))
TEST_ACCOUNT_REFRESH_TOKEN = "-".join(("oauth", "refresh", "test", "token", "123456789"))
CURRENT_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "b" * 64
OUTDATED_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "a" * 64
TEST_ASSISTANT_VERSION = "0.1.0"


def _routine_books(controller) -> None:
    """The recording turns, cards, and run protection a controller shares with its chat service (ADR-0101)."""
    controller.routine_recordings = local_routine_recorder.RecordingBook()
    controller.routine_proposals = local_routine_proposal.ProposalBook()
    controller.routine_protections = local_routine_protection.RunProtections()


LOOKUP_INPUT = {"page": 1, "per_page": 25}
LOOKUP_RESULT = {
    "zones": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}


def chat_body(
    message: str,
    *,
    assistant_ids: list[str] | tuple[str, ...] = (),
    files: list[str] | tuple[str, ...] = (),
    conversation: list[object] | tuple[object, ...] = (),
    locale: object = None,
    timezone: object = None,
) -> dict[str, object]:
    """One complete Local chat request body under the fixed test request identity."""
    return {
        "message": message,
        "files": list(files),
        "assistant_ids": list(assistant_ids),
        "conversation": list(conversation),
        "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
        "timezone": timezone,
        "locale": locale,
    }


def invalid_listing() -> dict[str, object]:
    """The inventory listing of the harness's published Cloudflare Assistant once it fails admission."""
    return {
        "assistants": [
            {
                "assistant": "shimpz-cloudflare",
                "assistant_version": TEST_ASSISTANT_VERSION,
                "status": "invalid",
                "provenance": "published",
            }
        ]
    }


class TestAssistantRegistry(dict):
    def get(self, team_id, assistant_id=None):
        return super().get(team_id if assistant_id is None else assistant_id)

    def all(self):
        return tuple(self.values())

    def identities(self):
        return {("team_1", assistant_id) for assistant_id in self}

    def delete(self, _team_id, assistant_id):
        return self.pop(assistant_id, None) is not None

    def binding(self, _team_id, assistant_id):
        return None

    @staticmethod
    def versioned(binding):
        return binding, TEST_ASSISTANT_VERSION

    def team_bindings(self, _team_id):
        return tuple(self.values())

    @staticmethod
    def spec(binding):
        return binding


class LocalContractCase(unittest.TestCase):
    def _registry(self, image: str) -> dict[str, AssistantSpec]:
        return TestAssistantRegistry({"shimpz-cloudflare": assistant_spec(image)})

    def _chat_controller(
        self,
        directory: str,
        runtime,
    ) -> local_app.LocalController:
        image = "sha256:" + "a" * 64
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.registry = self._registry(image)
        controller.registry["shimpz-cloudflare"] = replace(
            controller.registry["shimpz-cloudflare"],
            provenance="local",
            platform="linux/amd64",
        )
        controller.storage = SimpleNamespace(
            metadata=lambda _team_id, _files, _connection=None: [],
            metadata_connection=lambda _team_id, _files: nullcontext(None),
            settle=lambda _team_id, _files: None,
        )
        controller.routine_store = local_routine_store.RoutineStore(
            Path(directory) / "routines" / "state", Path(directory) / "routines" / "key" / "aes256.key"
        )
        controller.routine_diagnostics = local_routine_diagnostics.DiagnosticStore(
            Path(directory) / "routines" / "state" / "diagnostics",
            Path(directory) / "routines" / "key" / "diagnostics.key",
        )
        controller.inference_store = inference_config.InferenceConfigStore(Path(directory) / "inference")
        controller.inference_store.save(
            "team_1",
            inference_config.normalize("openai", "gpt-6-luna"),
        )
        controller.brain_runtime = runtime
        controller.action_state = local_app.action_journal.ActionJournal(
            Path(directory) / "action-journal" / "journal.sqlite3"
        )
        self.addCleanup(controller.action_state.close)
        controller.assistant_integrations = integration_store.OAuthIntegrationStore(
            Path(directory) / "assistant-integrations" / "state" / "integrations.json",
            Path(directory) / "assistant-integrations" / "key" / "aes256.key",
        )
        controller.assistant_stored_inputs = action_stored_input.StoredInputStore(
            Path(directory) / "assistant-stored-inputs" / "state" / "stored-inputs.json",
            Path(directory) / "assistant-stored-inputs" / "key" / "aes256.key",
        )
        controller.integration_challenges = integration_challenges.IntegrationChallengeStore()
        controller.oauth_pkce = integration_pkce.OAuthPKCEChallengeStore()
        controller.chat_continuations = local_chat_continuation_store.EncryptedContinuationStore(
            Path(directory) / "chat-continuations" / "state" / "continuations.json",
            Path(directory) / "chat-continuations" / "key" / "aes256.key",
        )
        integration = controller.registry["shimpz-cloudflare"].integrations["cloudflare"]
        controller.assistant_integrations.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            integration.provider,
            integration.scopes,
            SimpleNamespace(
                access_token=TEST_ACCOUNT_ACCESS_TOKEN,
                refresh_token=TEST_ACCOUNT_REFRESH_TOKEN,
                scopes=integration.scopes,
                expires_in=3600,
            ),
        )
        controller._locks = tuple(threading.RLock() for _ in range(64))
        controller._names_lock = threading.RLock()
        controller.team_names = local_names.TeamNameStore(Path(directory) / "inference")
        _routine_books(controller)
        controller._wire_collaborators()
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda _container, spec: tuple(
            sorted(spec.allowed_hosts)
        )
        controller.assistant_lifecycle._assistant_language = lambda _active: FIXTURE_PACK
        container = SimpleNamespace(id="assistant-container", status="running", reload=lambda: None)
        network = SimpleNamespace(id="a" * 64, name="team-network")
        controller.assistant_lifecycle._network = lambda _team_id: network
        controller.assistant_lifecycle._validate_network = lambda _network, _team_id, **_kwargs: "Marketing"
        controller.assistant_lifecycle._assistant_container = lambda _team_id, _assistant: container
        controller.assistant_lifecycle._validate_container = lambda *_args: None
        controller.chat_turn_service._active_chat_assistants = lambda _team_id, _network: (
            ActiveAssistant(controller.registry["shimpz-cloudflare"], container.id, container),
        )
        controller.assistant_lifecycle._active_assistant_genesis = lambda _active: (
            "Use only the declared Cloudflare Actions."
        )
        controller.chat_turn_service._restore_all_chat_continuations()
        return controller

    def _lifecycle_controller(self) -> tuple[local_app.LocalController, SimpleNamespace, list[object]]:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.cpuset_cpus = "0"
        controller._locks = tuple(threading.RLock() for _ in range(64))
        state_directory = tempfile.TemporaryDirectory()
        self.addCleanup(state_directory.cleanup)
        controller._names_lock = threading.RLock()
        controller.team_names = local_names.TeamNameStore(Path(state_directory.name) / "inference")
        controller.assistant_integrations = integration_store.OAuthIntegrationStore(
            Path(state_directory.name) / "assistant-integrations" / "state" / "integrations.json",
            Path(state_directory.name) / "assistant-integrations" / "key" / "aes256.key",
        )
        controller.assistant_stored_inputs = action_stored_input.StoredInputStore(
            Path(state_directory.name) / "assistant-stored-inputs" / "state" / "stored-inputs.json",
            Path(state_directory.name) / "assistant-stored-inputs" / "key" / "aes256.key",
        )
        controller.integration_challenges = integration_challenges.IntegrationChallengeStore()
        controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
        controller.oauth_pkce = integration_pkce.OAuthPKCEChallengeStore()
        spec = SimpleNamespace(
            assistant_id="shimpz-cloudflare",
            image=CURRENT_ASSISTANT_IMAGE,
            provenance="published",
            allowed_hosts=(),
            integrations={},
            stored_inputs={},
        )
        controller.registry = TestAssistantRegistry({spec.assistant_id: spec})
        _routine_books(controller)
        controller._wire_collaborators()
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda _container, spec: tuple(
            sorted(spec.allowed_hosts)
        )
        controller.assistant_lifecycle._assistant_language = lambda _active: FIXTURE_PACK
        controller.assistant_lifecycle._read_admitted_egress_policy = lambda *_args: None
        network_name = controller.assistant_lifecycle._network_name("team_1")
        network = SimpleNamespace(name=network_name)
        controller.assistant_lifecycle._network = lambda _team_id: network
        labels = controller.assistant_lifecycle._assistant_labels("team_1", spec)
        labels[local_app.IMAGE_LABEL] = OUTDATED_ASSISTANT_IMAGE
        container = SimpleNamespace(
            id="assistant-container",
            name=controller.assistant_lifecycle._container_name("team_1", spec.assistant_id),
            status="running",
            labels=labels,
            attrs={
                "Image": "sha256:" + "a" * 64,
                "Config": {
                    "Labels": labels,
                    "Image": OUTDATED_ASSISTANT_IMAGE,
                    "User": action_execution.ASSISTANT_RPC_USER,
                    "Env": [],
                },
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "CapAdd": None,
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Privileged": False,
                    "NetworkMode": network_name,
                    "Memory": assistant_lifecycle.ASSISTANT_MEMORY,
                    "MemorySwap": assistant_lifecycle.ASSISTANT_MEMORY,
                    "NanoCpus": assistant_lifecycle.ASSISTANT_NANO_CPUS,
                    "CpusetCpus": controller.cpuset_cpus,
                    "PidsLimit": assistant_lifecycle.ASSISTANT_PIDS,
                    "IpcMode": "private",
                    "CgroupnsMode": "private",
                    "Tmpfs": dict(assistant_lifecycle.ASSISTANT_TMPFS),
                    "Ulimits": list(assistant_lifecycle.ASSISTANT_ULIMITS),
                    "Sysctls": None,
                    "AutoRemove": False,
                    "RestartPolicy": {"Name": "no"},
                    "LogConfig": {
                        "Type": "none",
                        "Config": {},
                    },
                    "PortBindings": None,
                    "Binds": None,
                    "Devices": None,
                    "DeviceRequests": None,
                },
                "Mounts": [],
                "NetworkSettings": {"Networks": {network_name: {}}},
            },
        )
        container.reload = lambda: events.append("reload")
        container.remove = lambda *, force: events.append(("remove", force))
        controller.assistant_lifecycle._assistant_container = lambda *_args, **_kwargs: container
        controller.client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_kwargs: [container]))
        controller.assistant_lifecycle.client = controller.client
        controller.assistant_lifecycle._queue_residue = lambda image_id: events.append(("residue-add", image_id))
        controller.assistant_lifecycle.sweep_residues = lambda: events.append("residue-sweep")
        return controller, container, events
