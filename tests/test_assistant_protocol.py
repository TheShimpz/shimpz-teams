"""Independent Team conformance for the published Assistant manifest."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from assistant.manifest import (
    ManifestError,
    canonical_machine_contract,
    parse_manifest_contract,
    parse_manifest_genesis,
)
from tests import catalog_fixtures

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "vectors" / "manifest.json"
PROTOCOL = VECTORS.parents[1]
CLOSED_OBJECT = {"type": "object", "additionalProperties": False}
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz-developers",
    "commit": "fb99c5cb8ff7e654dd7ff229f807a944c86e16db",
    "path": "protocol/assistant/v1",
    "tree": "46cb764b121c7c929db6187d987b693fe0cc06d1",
    "contract_files_sha256": "add64d800a615b0e6b055bdc986ecf79188320156ccd17d3f703fceed37dc238",
}


class AssistantProtocolTests(unittest.TestCase):
    def test_protocol_mirror_matches_its_developers_pin(self) -> None:
        upstream = json.loads((PROTOCOL.parent / "upstream.json").read_bytes())
        self.assertEqual(upstream, EXPECTED_UPSTREAM)
        manifest = (PROTOCOL / "contract-files.sha256").read_bytes()
        self.assertEqual(hashlib.sha256(manifest).hexdigest(), EXPECTED_UPSTREAM["contract_files_sha256"])
        for line in manifest.decode("ascii").splitlines():
            expected, filename = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((PROTOCOL / filename).read_bytes()).hexdigest(), expected)

    def test_matches_every_published_manifest_vector(self) -> None:
        vectors = json.loads(VECTORS.read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            manifest = case["manifest"].encode()
            try:
                parse_manifest_contract(manifest)
                parse_manifest_genesis(manifest)
            except ManifestError:
                valid = False
            else:
                valid = True
            self.assertEqual(valid, case["valid"], case["name"])

    def test_matches_every_published_action_schema_vector_in_both_positions(self) -> None:
        vectors = json.loads((PROTOCOL / "vectors/action-schema.json").read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            for position in ("input_schema", "output_schema"):
                action = {
                    "id": "run",
                    "input_schema": CLOSED_OBJECT,
                    "output_schema": CLOSED_OBJECT,
                    "integrations": [],
                    "stored_inputs": [],
                    "input_files": [],
                    "human_requests": [],
                    "effect": "read_only",
                }
                action[position] = case["schema"]
                try:
                    canonical_machine_contract(
                        {"version": 1, "actions": [action], "messages": catalog_fixtures.messages()},
                        (),
                        summary=catalog_fixtures.SUMMARY,
                        allowed_hosts=(),
                    )
                except ManifestError:
                    valid = False
                else:
                    valid = True
                self.assertEqual(valid, case["valid"], f"{case['name']} as {position}")


if __name__ == "__main__":
    unittest.main()
