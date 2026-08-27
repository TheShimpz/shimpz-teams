"""Fast contracts for the deterministic Local controller Docker fixture."""

from __future__ import annotations

import json
import sys
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from local_controller_docker_fixture import FIXTURE, fixture_resolution


class LocalControllerDockerFixtureTest(unittest.TestCase):
    def test_resolution_matches_package_stored_inputs_without_foreign_residue(self) -> None:
        flow = SimpleNamespace(
            trusted_ref="127.0.0.1:5000/shimpz/shimpz-cloudflare@sha256:" + ("a" * 64),
            source_digest="",
        )

        resolution = fixture_resolution(flow)
        manifest = tomllib.loads((FIXTURE / "shimpz.toml").read_text(encoding="utf-8"))
        manifest_ids = set(manifest.get("stored_inputs", {}))
        declared_ids = {item["id"] for item in resolution["stored_inputs"]}
        referenced_ids = {
            stored_input
            for action in resolution["machine_contract"]["actions"]
            for stored_input in action["stored_inputs"]
        }

        self.assertEqual(declared_ids, manifest_ids)
        self.assertLessEqual(referenced_ids, declared_ids)
        self.assertNotIn("whatsapp", json.dumps(resolution).lower())


if __name__ == "__main__":
    unittest.main()
