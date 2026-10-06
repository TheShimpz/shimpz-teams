"""Integrity gate for the producer-owned Team Action protocol that the Brain mirrors."""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

from assistant import action_schema
from protocol.action.v1 import schema as action_protocol

PROTOCOL = Path(__file__).resolve().parents[1] / "protocol" / "action" / "v1"
MANIFEST = PROTOCOL / "contract-files.sha256"
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


class TeamActionProtocolTests(unittest.TestCase):
    def test_manifest_covers_and_digests_every_produced_artifact(self) -> None:
        matches = [ROW.fullmatch(line) for line in MANIFEST.read_text(encoding="ascii").splitlines()]
        self.assertTrue(matches)
        self.assertTrue(all(matches))
        expected = {match[2]: match[1] for match in matches if match is not None}
        actual = {path.name for path in PROTOCOL.iterdir() if path.is_file() and path != MANIFEST}
        self.assertEqual(set(expected), actual)
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((PROTOCOL / filename).read_bytes()).hexdigest(), digest)

    def test_team_admission_applies_the_protocol_bounds_and_matcher(self) -> None:
        self.assertEqual((action_schema.MAX_NODES, action_schema.MAX_BYTES), (4096, 128 * 1024))
        self.assertEqual(
            (action_schema.MAX_NODES, action_schema.MAX_BYTES), (action_protocol.MAX_NODES, action_protocol.MAX_BYTES)
        )
        self.assertIs(action_schema.pattern_matches, action_protocol.pattern_matches)


if __name__ == "__main__":
    unittest.main()
