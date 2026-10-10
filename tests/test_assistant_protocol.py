"""Independent Team conformance for the published Assistant manifest."""

import hashlib
import json
import unittest
from pathlib import Path

from assistant.manifest import (
    ManifestError,
    canonical_machine_contract,
    canonical_stored_input_declarations,
    parse_manifest_contract,
    parse_manifest_genesis,
)
from tests import catalog_fixtures

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "vectors" / "manifest.json"
PROTOCOL = VECTORS.parents[1]
CLOSED_OBJECT = {"type": "object", "additionalProperties": False}
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz-developers",
    "commit": "35f92a09afe39f17c79833ca33d0578b6b13cf18",
    "path": "protocol/assistant/v1",
    "tree": "8263084cd58c3e386757ac0693d8e8c5623f196a",
    "contract_files_sha256": "1d27ac2da50b40dc6f436b9daef15e548438083f137f96c04c7e5d4990511ab9",
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

    def test_admits_stored_input_routes_exactly_as_every_published_route_vector(self) -> None:
        vectors = json.loads((PROTOCOL / "vectors/route.json").read_bytes())
        self.assertEqual(vectors["version"], 1)
        declaration = {
            "kind": "password",
            "label": "Token",
            "description": catalog_fixtures.STORED_INPUT_HELP,
            "help_url": catalog_fixtures.HELP_URL,
            "host": "api.example.com",
            "header": "X-Api-Key",
        }
        for case in vectors["cases"]:
            try:
                admitted = canonical_stored_input_declarations(
                    {"token": {**declaration, "routes": case["routes"]}}, ("api.example.com",)
                )
            except ManifestError:
                valid = False
            else:
                valid = True
                self.assertEqual(admitted[0].routes, case["routes"], case["name"])
            self.assertEqual(valid, case["valid"], case["name"])

    def test_matches_every_published_action_schema_vector_in_both_positions(self) -> None:
        vectors = json.loads((PROTOCOL / "vectors/action-schema.json").read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            for position in ("input_schema", "output_schema"):
                action = {
                    "id": "run",
                    "description": catalog_fixtures.ACTION_DESCRIPTION,
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
                        description=catalog_fixtures.ASSISTANT_DESCRIPTION,
                        allowed_hosts=(),
                    )
                except ManifestError:
                    valid = False
                else:
                    valid = True
                self.assertEqual(valid, case["valid"], f"{case['name']} as {position}")


if __name__ == "__main__":
    unittest.main()
