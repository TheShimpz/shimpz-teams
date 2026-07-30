"""Pin the byte-identical Developers-to-Controller v1 authority."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "protocol" / "install"
AUTHORITY = ROOT / "v1"
MANIFEST = AUTHORITY / "contract-files.sha256"
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz",
    "commit": "fcffe20b89460c03cdd205ea3385bfa6085899d4",
    "path": "standards/assistant-install/v1",
    "tree": "296ae30a4a28f8be9448589869aa485a35bd767d",
    "contract_files_sha256": "8eb6795414de89fd4470dd54bb254588f071d76bfa349c4b0b8a940888d49688",
}
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


class DevelopersControllerContractTests(unittest.TestCase):
    def test_vendored_authority_matches_pinned_umbrella_tree(self) -> None:
        upstream = json.loads((ROOT / "upstream.json").read_bytes())
        self.assertEqual(upstream, EXPECTED_UPSTREAM)
        self.assertEqual(
            hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
            EXPECTED_UPSTREAM["contract_files_sha256"],
        )
        rows = [ROW.fullmatch(line) for line in MANIFEST.read_text(encoding="ascii").splitlines()]
        self.assertTrue(all(rows))
        expected = {match[2]: match[1] for match in rows if match is not None}
        self.assertEqual(
            sorted(path.name for path in AUTHORITY.iterdir() if path.name != "__pycache__"),
            sorted([*expected, MANIFEST.name]),
        )
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((AUTHORITY / filename).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
