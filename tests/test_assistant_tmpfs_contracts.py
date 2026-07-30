"""Hosted and Local Assistant writable-temp contracts."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_assistant_fixture import hosted_spec
from local_controller_harness import LocalContractCase

from hosted import container as container_spec


class AssistantTmpfsContracts(LocalContractCase):
    def test_each_profile_applies_its_current_bounded_tmpfs(self):
        hosted = container_spec.build_assistant_kwargs(
            "team_1",
            "shimpz-cloudflare",
            hosted_spec("ghcr.io/example/example-assistant@sha256:" + ("a" * 64)),
            owner="account_1",
            source_digest="sha256:" + ("c" * 64),
        )["tmpfs"]
        controller, _existing, _events = self._lifecycle_controller()
        captured = {}
        created = SimpleNamespace(
            id="new",
            attrs={"Image": "img-id"},
            reload=lambda: None,
            start=lambda: None,
        )
        controller.assistant_lifecycle.client.containers.create = lambda **kwargs: captured.update(kwargs) or created
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda *_args: ()
        controller.assistant_lifecycle._validate_container = lambda *_args: None
        controller.assistant_lifecycle._wait_ready = lambda *_args: None
        controller.assistant_lifecycle._active_assistant_genesis = lambda *_args: None
        spec = controller.registry["shimpz-cloudflare"]
        network = controller.assistant_lifecycle._network("team_1")

        controller.assistant_lifecycle._create_assistant_container(
            "team_1",
            spec,
            network,
            SimpleNamespace(id="img-id"),
        )

        self.assertEqual(hosted, {container_spec.CONTAINER_TMP: "size=64m,mode=1777"})
        self.assertEqual(captured["tmpfs"], {container_spec.CONTAINER_TMP: "size=256m"})
