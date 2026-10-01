"""Integrity gate for the producer-owned Team HTTP protocol."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from protocol.http.v1 import payload, routine

PROTOCOL = Path(__file__).resolve().parents[1] / "protocol" / "http" / "v1"
MANIFEST = PROTOCOL / "contract-files.sha256"
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


class TeamHttpProtocolTests(unittest.TestCase):
    def test_manifest_covers_and_digests_every_produced_artifact(self) -> None:
        matches = [ROW.fullmatch(line) for line in MANIFEST.read_text(encoding="ascii").splitlines()]
        self.assertTrue(matches)
        self.assertTrue(all(matches))
        expected = {match[2]: match[1] for match in matches if match is not None}
        actual = {path.name for path in PROTOCOL.iterdir() if path.is_file() and path != MANIFEST}
        self.assertEqual(set(expected), actual)
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((PROTOCOL / filename).read_bytes()).hexdigest(), digest)


class LocalizedChallengeContractTests(unittest.TestCase):
    """The ADR-0091 localization fields of a human challenge and a Routine challenge opening."""

    def setUp(self) -> None:
        self.vectors = json.loads((PROTOCOL / "vectors.json").read_bytes())

    def test_rendered_copy_admits_exactly_the_request_copy_fields(self) -> None:
        for case in self.vectors["rendered_copy"]["valid"]:
            self.assertEqual(payload.canonical_rendered(case["rendered"], case["request"]), case["rendered"])
        for case in self.vectors["rendered_copy"]["invalid"]:
            self.assertIsNone(payload.canonical_rendered(case["rendered"], case["request"]))

    def test_pack_digest_and_challenge_open_locale_are_closed(self) -> None:
        for name, admit in (
            ("pack_digest", payload.canonical_pack_digest),
            ("routine_challenge_open", routine.canonical_challenge_open),
        ):
            for value in self.vectors[name]["valid"]:
                self.assertEqual(admit(value), value)
            for value in self.vectors[name]["invalid"]:
                self.assertIsNone(admit(value))


if __name__ == "__main__":
    unittest.main()
