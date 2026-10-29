"""Team's canonical JSON bytes are the Shimpz canonical JSON v1 vectors of the Team HTTP protocol."""

import json
import unittest
from pathlib import Path

from core import canonical_json

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "http" / "v1" / "vectors.json"


class CanonicalJsonTests(unittest.TestCase):
    def test_encode_writes_every_canonical_vector_byte_for_byte(self) -> None:
        cases = json.loads(VECTORS.read_bytes())["canonical_json"]["canonical"]
        self.assertGreater(len(cases), 100)
        for case in cases:
            with self.subTest(utf8=case["utf8"]):
                self.assertEqual(canonical_json.encode(case["value"]), case["utf8"].encode())
