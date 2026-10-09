"""Local Assistant writable-temp contract."""

import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_controller_harness import LocalContractCase


class AssistantTmpfsContracts(LocalContractCase):
    def test_an_assistant_applies_its_current_bounded_tmpfs(self):
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

        self.assertEqual(captured["tmpfs"], {str(PurePosixPath("/") / "tmp"): "size=256m"})
