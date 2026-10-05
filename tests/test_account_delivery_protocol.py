"""Pin the byte-identical Account Integration-secret delivery v1 producer contract."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from protocol.account.delivery.v1 import aad as delivery_protocol

ROOT = Path(__file__).resolve().parents[1] / "protocol" / "account" / "delivery"
DELIVERY = ROOT / "v1"
MANIFEST = DELIVERY / "contract-files.sha256"
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz-account",
    "commit": "2d09c5e44db21b1c84534a5dc2f304f54ad35817",
    "path": "protocol/delivery/v1",
    "tree": "825966a0dc35e72b29750aec217ed1ce884e973d",
    "contract_files_sha256": "4e657f54583f6a84062a8dacc488df0fb65890b90082d82cc869ec14fa52199d",
}
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


class AccountDeliveryProtocolTests(unittest.TestCase):
    def test_mirror_matches_the_exact_account_commit_and_tree(self) -> None:
        upstream = json.loads((ROOT / "upstream.json").read_bytes())
        self.assertEqual(upstream, EXPECTED_UPSTREAM)
        self.assertEqual(hashlib.sha256(MANIFEST.read_bytes()).hexdigest(), EXPECTED_UPSTREAM["contract_files_sha256"])
        rows = [ROW.fullmatch(line) for line in MANIFEST.read_text(encoding="ascii").splitlines()]
        self.assertTrue(all(rows))
        expected = {match[2]: match[1] for match in rows if match is not None}
        self.assertEqual(
            sorted(path.name for path in DELIVERY.iterdir() if path.name != "__pycache__"),
            sorted([*expected, MANIFEST.name]),
        )
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((DELIVERY / filename).read_bytes()).hexdigest(), digest)

    def test_the_consumed_module_reproduces_every_producer_vector(self) -> None:
        document = json.loads((DELIVERY / "vectors.json").read_bytes())
        self.assertGreaterEqual(len(document["vectors"]), 2)
        for vector in document["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(
                    delivery_protocol.delivery_aad(
                        vector["account_id"],
                        vector["provider"],
                        vector["auth_type"],
                        bytes.fromhex(vector["recipient_public_key_hex"]),
                        bytes.fromhex(vector["sender_public_key_hex"]),
                    ),
                    vector["aad"].encode("ascii"),
                )


if __name__ == "__main__":
    unittest.main()
