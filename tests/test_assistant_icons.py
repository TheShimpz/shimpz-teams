"""Canonical Assistant icon custody contracts."""

import hashlib
import tempfile
import threading
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


BINDING = DynamicAssistantBinding(
    team_id="team_1",
    binding_digest="sha256:" + ("b" * 64),
    provenance="published",
    document={**resolution(), "assistant_id": "example"},
)


def bound(*bindings: DynamicAssistantBinding):
    return lambda: bindings


def keep(store: AssistantIconStore, value: dict[str, str] | None = None, contents: bytes = ICON) -> None:
    """Retain an icon whose binding commits, so it stays after the install releases its pin."""
    with store.retained(value or resolution(), contents, bound(BINDING)):
        pass


class AssistantIconStoreTests(unittest.TestCase):
    def test_persists_and_revalidates_exact_icon_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AssistantIconStore(Path(directory) / "icons")
            keep(store)

            self.assertEqual(store.read(resolution()), ICON)
            keep(store)

            with (
                mock.patch.object(store, "_read", return_value=b"other"),
                self.assertRaisesRegex(AssistantIconError, "conflicts"),
            ):
                keep(store)
            self.assertEqual(store._pins, {})

    def test_rejects_digest_mismatch_oversize_and_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "icons"
            store = AssistantIconStore(root)
            with self.assertRaisesRegex(AssistantIconError, "digest does not match"):
                keep(store, contents=b"different")
            with self.assertRaisesRegex(AssistantIconError, "invalid"):
                keep(store, resolution(b"x" * (1024 * 1024 + 1)), b"x" * (1024 * 1024 + 1))

            keep(store)
            next(root.glob("*.png")).write_bytes(b"tampered")
            with self.assertRaisesRegex(AssistantIconError, "digest does not match"):
                store.read(resolution())

    def test_removes_only_icons_without_a_remaining_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "icons"
            store = AssistantIconStore(root)
            keep(store)

            store.discard_unreferenced(SOURCE_DIGEST, bound(BINDING))
            self.assertEqual(store.read(resolution()), ICON)
            store.discard_unreferenced(SOURCE_DIGEST, bound())
            with self.assertRaisesRegex(AssistantIconError, "unavailable"):
                store.read(resolution())
            # An install whose binding never commits leaves no icon behind.
            with store.retained(resolution(), ICON, bound()):
                self.assertEqual(store.read(resolution()), ICON)
            self.assertEqual(list(root.glob("*.png")), [])

    def test_an_install_in_flight_keeps_its_icon_from_another_teams_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AssistantIconStore(Path(directory) / "icons")
            committed: list[DynamicAssistantBinding] = []
            pinned = threading.Event()
            cleaned = threading.Event()
            failures: list[AssistantIconError] = []

            def install() -> None:
                try:
                    with store.retained(resolution(), ICON, lambda: tuple(committed)):
                        pinned.set()
                        # Another Team's failed install or uninstall runs while this binding is not committed yet.
                        cleaned.wait(10)
                        committed.append(BINDING)
                except AssistantIconError as exc:
                    failures.append(exc)

            def other_team_cleanup() -> None:
                pinned.wait(10)
                store.discard_unreferenced(SOURCE_DIGEST, bound())
                cleaned.set()

            threads = [threading.Thread(target=install), threading.Thread(target=other_team_cleanup)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

            self.assertEqual(failures, [])
            self.assertTrue(cleaned.is_set())
            self.assertEqual(store.read(resolution()), ICON)
            self.assertEqual(store._pins, {})
            # Once the binding is gone and nothing is in flight, the same cleanup removes the icon.
            committed.clear()
            store.discard_unreferenced(SOURCE_DIGEST, lambda: tuple(committed))
            with self.assertRaisesRegex(AssistantIconError, "unavailable"):
                store.read(resolution())

    def test_the_icon_stays_while_any_install_of_it_is_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "icons"
            store = AssistantIconStore(root)
            with store.retained(resolution(), ICON, bound()):
                with store.retained(resolution(), ICON, bound()):
                    pass
                # The first install failed, but the second still holds the icon it is about to bind.
                self.assertEqual(store.read(resolution()), ICON)
            self.assertEqual(list(root.glob("*.png")), [])
            self.assertEqual(store._pins, {})

    def test_deletion_reads_the_bindings_inside_the_custody_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AssistantIconStore(Path(directory) / "icons")
            keep(store)
            observed: list[bool] = []

            def references():
                observed.append(store._custody.locked())
                return (BINDING,)

            store.discard_unreferenced(SOURCE_DIGEST, references)
            self.assertEqual(observed, [True])
            self.assertEqual(store.read(resolution()), ICON)

    def test_local_image_identity_cannot_collide_with_a_publication_source(self) -> None:
        local_icon = b"different local icon"
        local_record = {
            "image_id": SOURCE_DIGEST,
            "icon_digest": f"sha256:{hashlib.sha256(local_icon).hexdigest()}",
            "assistant_id": "example",
        }
        local_binding = DynamicAssistantBinding(
            team_id="team_1",
            binding_digest="sha256:" + ("c" * 64),
            provenance="local",
            document=local_record,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "icons"
            store = AssistantIconStore(root)
            keep(store)
            with store.retained_local(local_record, local_icon, bound(local_binding)):
                pass

            self.assertEqual(store.read(resolution()), ICON)
            self.assertEqual(store.read_binding(local_binding), local_icon)
            self.assertEqual(
                {path.name for path in root.iterdir()},
                {
                    f"published-{SOURCE_DIGEST.removeprefix('sha256:')}.png",
                    f"local-{SOURCE_DIGEST.removeprefix('sha256:')}.png",
                },
            )
            store.discard_binding(local_binding, bound(local_binding))
            self.assertEqual(store.read_binding(local_binding), local_icon)
            store.discard_binding(local_binding, bound())
            self.assertEqual(store.read(resolution()), ICON)

    def test_local_api_serves_only_an_installed_verified_icon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AssistantIconStore(Path(directory) / "icons")
            keep(store)
            controller = SimpleNamespace(
                _lock=lambda _team_id: nullcontext(),
                registry=SimpleNamespace(binding=lambda _team_id, _assistant_id: BINDING),
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
        with self.assertRaisesRegex(AssistantIconError, "Local Assistant icon identity"):
            store.retained_local({"image_id": "invalid", "icon_digest": SOURCE_DIGEST}, ICON, bound())
        invalid_binding = DynamicAssistantBinding("team_1", SOURCE_DIGEST, "unknown", {})
        with self.assertRaisesRegex(AssistantIconError, "provenance is invalid"):
            store.read_binding(invalid_binding)
        with self.assertRaisesRegex(AssistantIconError, "icon key"):
            store._path(SimpleNamespace(namespace="published", key="invalid"))

        with (
            mock.patch.object(Path, "unlink", side_effect=OSError("read-only")),
            self.assertRaisesRegex(AssistantIconError, "cannot be removed"),
        ):
            store.discard_unreferenced(SOURCE_DIGEST, bound())

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
