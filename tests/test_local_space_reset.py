from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import CURRENT_ASSISTANT_IMAGE, LocalContractCase, TestAssistantRegistry

from local import app as local_app

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
    "stored_inputs",
    "team_networks",
    "team_storage",
]


class LocalSpaceResetTests(LocalContractCase):
    def test_reset_removes_orphan_egress_authority_for_owned_teams(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.chat_continuations = SimpleNamespace(clear=lambda: 0)
        controller._locks = (threading.RLock(),)
        controller.registry = TestAssistantRegistry({"shimpz-cloudflare": SimpleNamespace()})
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
        controller.chat_turn_service._delete_all_stored_input_state = lambda: events.append("delete-stored-inputs")
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
        self.assertEqual(result["residue_absent"], LOCAL_TEAM_RESIDUES)
        self.assertIn(("remove-policy", "team_1", "shimpz-cloudflare"), events)
        self.assertIn(("purge-action", "a" * 64), events)
        self.assertIn("residue-sweep", events)
        self.assertEqual(controller.registry.identities(), set())
        self.assertLess(events.index("delete-integrations"), events.index("network-remove"))
        self.assertLess(events.index("delete-stored-inputs"), events.index("network-remove"))

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
            provenance="published",
        )
        controller.registry = TestAssistantRegistry({spec.assistant_id: spec})
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
        controller.chat_turn_service._delete_all_stored_input_state = lambda: events.append("delete-stored-inputs")
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
