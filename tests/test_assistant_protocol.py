"""Independent Team conformance for the published Assistant manifest."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from assistant.manifest import ManifestError, parse_manifest_contract, parse_manifest_genesis

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "manifest-vectors.json"
PROTOCOL = VECTORS.parent
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz-developers",
    "commit": "06f7553afa9610c70bfc1b61ab3a2172893cd581",
    "path": "protocol/assistant/v1",
    "tree": "ad182db7ce62242b93fab66735263f9dd15ceb19",
    "contract_files_sha256": "2307bafc64fdf7056dbe0dc6527496a782bdf5502c2eb5f635d5829fd99f82c8",
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


if __name__ == "__main__":
    unittest.main()
