import unittest

from assistant import genesis as assistant_genesis
from tests.assistant_archive_fixtures import ManifestContainer as Container


def manifest(genesis: str = "Use only the declared Actions.") -> bytes:
    return f"""
[shimpz]
spec = 1
id = "fixture"
version = "0.1.0"
name = "Fixture"
summary = "Exercise Genesis admission."
description = "Exercise the Genesis admission of one fixture Assistant."
creators = ["@fixture"]
github = "https://github.com/TheShimpz/fixture"
genesis = \"\"\"
{genesis}
\"\"\"

[network]
allowed_hosts = []
""".encode()


class AssistantGenesisTests(unittest.TestCase):
    def test_reads_genesis_from_the_spec_v1_manifest(self) -> None:
        container = Container("generation", manifest("Inspect DNS safely.\n"))

        self.assertEqual(assistant_genesis.read_container_genesis(container), "Inspect DNS safely.")
        self.assertEqual(container.reads, 1)

    def test_invalid_manifest_fails_without_echoing_it(self) -> None:
        private = "private-value-123456789"
        container = Container("generation", manifest(private) + b"unknown = true\n")

        with self.assertRaises(assistant_genesis.GenesisError) as caught:
            assistant_genesis.read_container_genesis(container)
        self.assertNotIn(private, str(caught.exception))

    def test_cache_reads_each_immutable_generation_once(self) -> None:
        first = Container("first", manifest("First."))
        second = Container("second", manifest("Second."))
        cache = assistant_genesis.GenesisCache(max_entries=1)

        self.assertEqual(cache.get(first), "First.")
        self.assertEqual(cache.get(first), "First.")
        self.assertEqual(first.reads, 1)
        self.assertEqual(cache.get(second), "Second.")
        self.assertEqual(cache.get(first), "First.")
        self.assertEqual(first.reads, 2)


if __name__ == "__main__":
    unittest.main()
