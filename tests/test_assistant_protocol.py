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

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "manifest-vectors.json"
PROTOCOL = VECTORS.parent
CLOSED_OBJECT = {"type": "object", "additionalProperties": False}
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz-developers",
    "commit": "ca49b79b10e766fd94f92be86dabd04a4a9fb1d1",
    "path": "protocol/assistant/v1",
    "tree": "b9d0c4498350ea418122703e990d1cda31f9f76d",
    "contract_files_sha256": "7b1a1c0d13b4a9cdbecd04b8878979c2d3fe7139a73d5d421d0d29214b92e9f7",
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
        vectors = json.loads((PROTOCOL / "action-schema-vectors.json").read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            for position in ("input_schema", "output_schema"):
                action = {
                    "id": "run",
                    "input_schema": CLOSED_OBJECT,
                    "output_schema": CLOSED_OBJECT,
                    "integrations": [],
                    "stored_inputs": [],
                    "human_requests": [],
                }
                action[position] = case["schema"]
                try:
                    canonical_machine_contract({"version": 1, "actions": [action]}, ())
                except ManifestError:
                    valid = False
                else:
                    valid = True
                self.assertEqual(valid, case["valid"], f"{case['name']} as {position}")


if __name__ == "__main__":
    unittest.main()
