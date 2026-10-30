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
    "commit": "e1c44362e0f1d2dd1bfe8dfe0080596e86bfd0e5",
    "path": ".standards/assistant-install/v1",
    "tree": "04c38ee1e61fee5cfad6a43ffd0815dbaed24ddf",
    "contract_files_sha256": "6c9722837fd1ce9df14e5eb4240e4e643ee0de65f35bcc3c7163456014958cfd",
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
