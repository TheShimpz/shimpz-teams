"""Pin the byte-identical Assistant-install v1 authority."""

import hashlib
import json
import unittest
from pathlib import Path

from protocol_manifest import assert_manifest_holds

ROOT = Path(__file__).resolve().parents[1] / "protocol" / "install"
AUTHORITY = ROOT / "v1"
MANIFEST = AUTHORITY / "contract-files.sha256"
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/TheShimpz/shimpz",
    "commit": "907fb942d3832f802c274b8a1144ee9b04e060fd",
    "path": ".standards/assistant-install/v1",
    "tree": "1335bc109e7a631617cfd261daf66502408ed8b3",
    "contract_files_sha256": "a507dadb341cf28aacbd650ae8af1d6ba5649af97ed5084c0c5898832f0b4628",
}


class AssistantInstallProtocolTests(unittest.TestCase):
    def test_vendored_authority_matches_pinned_umbrella_tree(self) -> None:
        upstream = json.loads((ROOT / "upstream.json").read_bytes())
        self.assertEqual(upstream, EXPECTED_UPSTREAM)
        self.assertEqual(
            hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
            EXPECTED_UPSTREAM["contract_files_sha256"],
        )
        assert_manifest_holds(self, AUTHORITY)


if __name__ == "__main__":
    unittest.main()
