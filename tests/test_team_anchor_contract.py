from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from core.container import network as network_policy
from hosted import container as container_spec


class TeamAnchorContractTests(unittest.TestCase):
    def test_anchor_has_no_model_runtime_or_secret_bearing_filesystem(self) -> None:
        kwargs = container_spec.build_team_kwargs(
            "team_1",
            "Team 1",
        )

        self.assertIn("registry.k8s.io/pause:3.10.1@sha256:", kwargs["image"])
        self.assertEqual(kwargs["environment"], {"SHIMPZ_TEAM_ID": "team_1", "SHIMPZ_TEAM_NAME": "Team 1"})
        self.assertNotIn("postgresql://", repr(kwargs))
        self.assertNotIn("team.brain", kwargs["labels"])
        self.assertNotIn("team.model", kwargs["labels"])
        self.assertTrue(kwargs["read_only"])
        self.assertEqual(kwargs["cap_drop"], ["ALL"])
        self.assertEqual(kwargs["cap_add"], [])
        self.assertEqual(kwargs["mounts"], [])
        self.assertNotIn("healthcheck", kwargs)
        self.assertEqual(kwargs["network"], network_policy.network_name("team_1", network_policy.CORE_KIND))
        self.assertEqual(network_policy.NETWORK_KINDS, {network_policy.CORE_KIND})

    def test_anchor_reserves_only_a_small_idle_envelope(self) -> None:
        self.assertEqual(container_spec.MEM_LIMIT_BYTES, 64 * 1024 * 1024)
        self.assertEqual(container_spec.NANO_CPUS, 100_000_000)
        self.assertEqual(container_spec.PIDS_LIMIT, 128)


if __name__ == "__main__":
    unittest.main()
