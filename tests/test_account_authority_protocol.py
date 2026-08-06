"""Pin the byte-identical Account authority v1 producer contract."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "protocol" / "account" / "authority"
AUTHORITY = ROOT / "v1"
MANIFEST = AUTHORITY / "contract-files.sha256"
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz-account",
    "commit": "e956e5f096549ce22ed8c355564e818842d983c7",
    "path": "protocol/authority/v1",
    "tree": "3bd2f1b7732c7eaf785f67cba39bd566fb894ca0",
    "contract_files_sha256": "c74548982f061e47c4a6aedf723ba1309241950a80223c39c4604cbfc7a329b0",
}
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


class AccountAuthorityProtocolTests(unittest.TestCase):
    def test_mirror_matches_the_exact_account_commit_and_tree(self) -> None:
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
            sorted([*expected, MANIFEST.name, "verify.py"]),
        )
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((AUTHORITY / filename).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
