"""Canonical Assistant icon custody contracts."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from install.bindings import DynamicAssistantBinding
from install.icons import AssistantIconError, AssistantIconStore
from local import app as local_app

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
            store.put(resolution(), ICON)

            with (
                mock.patch.object(store, "read", return_value=b"other"),
                self.assertRaisesRegex(AssistantIconError, "conflicts"),
            ):
                store.put(resolution(), ICON)

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

    def test_local_api_serves_only_an_installed_verified_icon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AssistantIconStore(Path(directory) / "icons")
            store.put(resolution(), ICON)
            controller = SimpleNamespace(
                _lock=lambda _team_id: nullcontext(),
                registry=SimpleNamespace(
                    binding=lambda _team_id, _assistant_id: SimpleNamespace(resolution=resolution())
                ),
                assistant_icons=store,
            )

            self.assertEqual(
                local_app.local_assistant_api.assistant_icon(controller, "team_1", "example-assistant"),
                ICON,
            )

    def test_local_api_rejects_an_uninstalled_assistant(self) -> None:
        controller = SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            registry=SimpleNamespace(binding=lambda _team_id, _assistant_id: None),
            assistant_icons=mock.Mock(),
        )

        with self.assertRaises(local_app.ApiProblem) as caught:
            local_app.local_assistant_api.assistant_icon(controller, "team_1", "missing-assistant")

        self.assertEqual(caught.exception.status, 404)

    def test_read_rejects_nonregular_files_and_closes_the_descriptor(self) -> None:
        store = AssistantIconStore(Path("/icons"))
        metadata = SimpleNamespace(st_mode=0o040700, st_size=len(ICON))
        with (
            mock.patch("install.icons.os.open", return_value=3),
            mock.patch("install.icons.os.fstat", return_value=metadata),
            mock.patch("install.icons.os.close") as close,
            self.assertRaisesRegex(AssistantIconError, "invalid"),
        ):
            store.read(resolution())
        close.assert_called_once_with(3)

    def test_invalid_identities_paths_and_cleanup_failures_are_closed(self) -> None:
        store = AssistantIconStore(Path("/icons"))
        for invalid in (
            {"source_digest": 1, "icon_digest": resolution()["icon_digest"]},
            {"source_digest": SOURCE_DIGEST, "icon_digest": "invalid"},
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(AssistantIconError, "identity"):
                store.read(invalid)
        with self.assertRaisesRegex(AssistantIconError, "source digest"):
            store._path("invalid")

        with (
            mock.patch.object(Path, "unlink", side_effect=OSError("read-only")),
            self.assertRaisesRegex(AssistantIconError, "cannot be removed"),
        ):
            store.discard_unreferenced(SOURCE_DIGEST, [])

    def test_persistence_failure_removes_a_partial_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "icons")
            store = AssistantIconStore(root)
            temporary = Path(directory, "partial")
            stream = mock.MagicMock()
            stream.__enter__.return_value.name = str(temporary)
            stream.__enter__.return_value.flush.side_effect = OSError("full")
            with (
                mock.patch("install.icons.tempfile.NamedTemporaryFile", return_value=stream),
                mock.patch.object(Path, "unlink") as unlink,
                self.assertRaisesRegex(AssistantIconError, "cannot be persisted"),
            ):
                store._write(root / "icon.png", ICON)
            unlink.assert_called_once_with(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
