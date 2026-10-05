"""Reading a staged Local Assistant image's files and preview without starting it, failing closed."""

from __future__ import annotations

import unittest
from types import MappingProxyType
from unittest import mock

from docker.errors import DockerException

from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from local.install import snapshots
from tests import catalog_fixtures
from tests.local_snapshot_fixtures import IMAGE_ID
from tests.local_snapshot_fixtures import client as _client


class LocalSnapshotFileTests(unittest.TestCase):
    def test_previews_the_validated_icon_and_summaries_only_from_the_image_pack(self) -> None:
        client, _image_value, container = _client()

        value = snapshots.preview(client, IMAGE_ID)

        self.assertTrue(value.icon.startswith(b"\x89PNG"))
        self.assertEqual(
            [call.args[0] for call in container.get_archive.call_args_list],
            [
                assistant_manifest.MANIFEST_PATH,
                assistant_manifest.CONTRACT_PATH,
                assistant_language.PACK_PATH,
                snapshots.ICON_PATH,
            ],
        )
        # English is the catalog summary; every other interface language is that message's pack translation.
        self.assertEqual(set(value.summaries), {"ar", "de", "en", "es", "fr", "ja", "pt", "zh"})
        self.assertEqual(value.summaries["en"], "Exercise immutable admission.")
        self.assertEqual(value.summaries["pt"], "PT Exercise immutable admission.")
        self.assertIsInstance(value.summaries, MappingProxyType)
        container.start.assert_not_called()
        container.remove.assert_called_once_with(force=True, v=False)

    def test_preview_refuses_a_missing_or_foreign_pack_and_cleans_up(self) -> None:
        foreign = catalog_fixtures.pack_bytes(catalog_fixtures.messages("Another summary."))
        for name, pack in (("missing", None), ("foreign", foreign), ("malformed", b"{}")):
            client, _image_value, container = _client(pack=pack)
            with self.subTest(pack=name):
                with self.assertRaises(snapshots.LocalSnapshotError) as raised:
                    snapshots.preview(client, IMAGE_ID)
                # An image without a file the stage contract requires is inadmissible, never a transient outage.
                self.assertNotIsInstance(raised.exception, snapshots.LocalSnapshotUnavailableError)
            container.remove.assert_called_once_with(force=True, v=False)

    def test_a_transient_archive_failure_is_unavailable_not_inadmissible(self) -> None:
        def broken_stream(path: str):
            def chunks():
                raise OSError("Docker closed the archive stream")
                yield b""

            name = path.rsplit("/", 1)[1]
            return chunks(), {"name": name, "size": 1, "mode": 0o444}

        failures = (
            ("daemon", DockerException("Docker is unavailable")),
            ("transport", OSError("Docker transport failed")),
            ("stream", broken_stream),
        )
        for name, failure in failures:
            for operation in (snapshots.preview, snapshots.admit):
                client, _image_value, container = _client()
                container.get_archive.side_effect = failure
                with (
                    self.subTest(failure=name, operation=operation.__name__),
                    self.assertRaises(snapshots.LocalSnapshotUnavailableError),
                ):
                    operation(client, IMAGE_ID)
                container.remove.assert_called_once_with(force=True, v=False)

        client, _image_value, _container = _client()
        client.containers.create.side_effect = DockerException("Docker is unavailable")
        with self.assertRaises(snapshots.LocalSnapshotUnavailableError):
            snapshots.preview(client, IMAGE_ID)

    def test_invalid_archive_metadata_is_inadmissible_not_unavailable(self) -> None:
        client, _image_value, container = _client()
        container.get_archive.side_effect = lambda path: (iter(()), {"name": "other", "size": 1, "mode": 0o444})
        with self.assertRaises(snapshots.LocalSnapshotError) as raised:
            snapshots.preview(client, IMAGE_ID)
        self.assertNotIsInstance(raised.exception, snapshots.LocalSnapshotUnavailableError)
        container.remove.assert_called_once_with(force=True, v=False)

    def test_preview_rejects_invalid_image_id_without_docker_access(self) -> None:
        client, _image_value, _container = _client()

        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "image id is invalid"):
            snapshots.preview(client, "latest")

        client.images.get.assert_not_called()

    def test_preview_rejects_invalid_declaration_and_cleans_up(self) -> None:
        client, _image_value, container = _client()

        with (
            mock.patch.object(
                snapshots.assistant_manifest,
                "parse_manifest_identity",
                side_effect=assistant_manifest.ManifestError("invalid"),
            ),
            self.assertRaisesRegex(snapshots.LocalSnapshotError, "preview is invalid"),
        ):
            snapshots.preview(client, IMAGE_ID)

        container.remove.assert_called_once_with(force=True, v=False)

    def test_preview_rejects_display_label_drift_and_always_cleans_up(self) -> None:
        client, image, container = _client()
        image.attrs["Config"]["Labels"][snapshots.NAME_LABEL] = "Different name"

        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "does not match"):
            snapshots.preview(client, IMAGE_ID)

        container.remove.assert_called_once_with(force=True, v=False)

    def test_extraction_and_cleanup_fail_closed(self) -> None:
        client, _image_value, container = _client()
        container.get_archive.side_effect = DockerException("unavailable")
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "could not be admitted"):
            snapshots.admit(client, IMAGE_ID)
        container.remove.assert_called_once_with(force=True, v=False)

        client, _image_value, container = _client()
        container.remove.side_effect = DockerException("unavailable")
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "could not be removed"):
            snapshots.admit(client, IMAGE_ID)

        self.assertIsNone(snapshots._remove_temporary_container(None))


if __name__ == "__main__":
    unittest.main()
