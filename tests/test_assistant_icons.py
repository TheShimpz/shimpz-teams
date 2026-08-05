"""Canonical Assistant icon custody contracts."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from install.bindings import DynamicAssistantBinding
from install.icons import AssistantIconError, AssistantIconStore

ICON = b"canonical icon bytes"
SOURCE_DIGEST = "sha256:" + ("a" * 64)


def resolution(contents: bytes = ICON) -> dict[str, str]:
    return {
        "source_digest": SOURCE_DIGEST,
        "icon_digest": f"sha256:{hashlib.sha256(contents).hexdigest()}",
    }


class AssistantIconStoreTests(unittest.TestCase):
    def test_persists_and_revalidates_exact_icon_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AssistantIconStore(Path(directory) / "icons")
            store.put(resolution(), ICON)

            self.assertEqual(store.read(resolution()), ICON)

    def test_rejects_digest_mismatch_oversize_and_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "icons"
            store = AssistantIconStore(root)
            with self.assertRaisesRegex(AssistantIconError, "digest does not match"):
                store.put(resolution(), b"different")
            with self.assertRaisesRegex(AssistantIconError, "invalid"):
                store.put(resolution(b"x" * (1024 * 1024 + 1)), b"x" * (1024 * 1024 + 1))

            store.put(resolution(), ICON)
            next(root.glob("*.png")).write_bytes(b"tampered")
            with self.assertRaisesRegex(AssistantIconError, "digest does not match"):
                store.read(resolution())

    def test_removes_only_icons_without_a_remaining_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "icons"
            store = AssistantIconStore(root)
            store.put(resolution(), ICON)
            binding = DynamicAssistantBinding(
                team_id="team_1",
                binding_digest="sha256:" + ("b" * 64),
                resolution={"source_digest": SOURCE_DIGEST, "assistant_id": "example"},
            )

            store.discard_unreferenced(SOURCE_DIGEST, [binding])
            self.assertEqual(store.read(resolution()), ICON)
            store.discard_unreferenced(SOURCE_DIGEST, [])
            with self.assertRaisesRegex(AssistantIconError, "unavailable"):
                store.read(resolution())


if __name__ == "__main__":
    unittest.main()
