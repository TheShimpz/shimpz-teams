import unittest
from pathlib import Path

from install.registry_auth import AnonymousRegistryAccess


class AnonymousRegistryAccessTests(unittest.TestCase):
    def test_anonymous_registry_access_writes_no_credentials(self) -> None:
        access = AnonymousRegistryAccess()

        self.assertEqual(repr(access), "AnonymousRegistryAccess()")
        self.assertIsNone(access.docker_auth_config())
        with access.docker_config() as directory:
            self.assertEqual(Path(directory, "config.json").read_text(encoding="ascii"), '{"auths":{}}')


if __name__ == "__main__":
    unittest.main()
