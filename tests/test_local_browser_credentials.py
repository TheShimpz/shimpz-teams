"""A controlled Browser's display credentials: KasmVNC's SHA-256-crypt hashes and the upgrade each stream carries."""

import base64
import unittest

from local.browser.credentials import Credentials, kasm_hash


class CredentialTests(unittest.TestCase):
    def test_the_hash_is_sha256_crypt_under_kasms_salt(self):
        # Vectors from `openssl passwd -5 -salt kasm`, the hash kasmvncpasswd writes.
        self.assertEqual(kasm_hash("viewerpass"), "$5$kasm$22E8Xs61cUGvV2nVcl8DbHiizSEilH1zFOaWAOxMzEA")
        self.assertEqual(
            kasm_hash("longer password with spaces"), "$5$kasm$GFmDoKMvmzCDcckA9YLL/eFES9k9C3kca4sX.2Dgb67"
        )
        self.assertEqual(kasm_hash("a" * 40), "$5$kasm$bww6oQ6V8NoXUx.nhJsThql1a01zxA/udVZ5PetgV9B")

    def test_each_browser_draws_its_own_pair_and_only_control_carries_the_writer(self):
        first, second = Credentials.draw(), Credentials.draw()
        self.assertNotEqual(first, second)
        self.assertNotEqual(first.viewer, first.controller)
        viewer, controller = first.file().decode().splitlines()
        self.assertEqual(viewer, f"viewer:{kasm_hash(first.viewer)}:r")
        self.assertEqual(controller, f"controller:{kasm_hash(first.controller)}:rw")
        watching = base64.b64decode(first.authorization(False).removeprefix("Basic ")).decode()
        controlling = base64.b64decode(first.authorization(True).removeprefix("Basic ")).decode()
        self.assertEqual(watching, f"viewer:{first.viewer}")
        self.assertEqual(controlling, f"controller:{first.controller}")


if __name__ == "__main__":
    unittest.main()
