"""Exercise fail-closed persistence edges for Assistant bindings and updates."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install import bindings, update
from install.contract import CONTRACT_ROOT

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]
IMAGE_ID = "sha256:" + "a" * 64


def _successor() -> dict[str, object]:
    value = copy.deepcopy(RESOLUTION)
    value["assistant_version"] = "0.2.0"
    value["source_digest"] = "sha256:" + "9" * 64
    return value


class BindingStoreEdgeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name, "bindings.json")
        self.store = bindings.DynamicAssistantStore(self.path)

    def test_capacity_invalid_fence_and_idempotent_replace_edges(self) -> None:
        with (
            mock.patch.object(bindings, "_MAX_BINDINGS", 0),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "full"),
        ):
            self.store.put("team_1", copy.deepcopy(RESOLUTION))

        current = self.store.put("team_1", copy.deepcopy(RESOLUTION))
        with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "digest is invalid"):
            self.store.replace("team_1", "invalid", copy.deepcopy(RESOLUTION))
        self.assertEqual(
            self.store.replace("team_1", current.binding_digest, copy.deepcopy(RESOLUTION)),
            current,
        )

    def test_registry_read_rejects_io_size_shape_and_duplicate_identities(self) -> None:
        with (
            mock.patch.object(Path, "read_bytes", side_effect=OSError("denied")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be read"),
        ):
            self.store._read()

        with mock.patch.object(bindings, "_MAX_FILE_BYTES", 1):
            self.path.write_bytes(b"{}")
            with self.assertRaisesRegex(bindings.DynamicAssistantError, "too large"):
                self.store._read()

        for document in ([], {}, {"version": 1, "bindings": []}, {"version": 2, "bindings": {}}):
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(bindings.DynamicAssistantError, "malformed"):
                    self.store._read()

        self.path.unlink()
        binding = self.store.put("team_1", copy.deepcopy(RESOLUTION))
        encoded = bindings._encode_binding(binding)
        self.path.write_text(json.dumps({"version": 2, "bindings": [encoded, encoded]}), encoding="utf-8")
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "duplicate"):
            self.store._read()

    def test_registry_write_rejects_size_and_cleans_partial_file(self) -> None:
        binding = bindings.binding_from_resolution("team_1", copy.deepcopy(RESOLUTION))
        with (
            mock.patch.object(bindings, "_MAX_FILE_BYTES", 1),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "too large"),
        ):
            self.store._write([binding])

        temporary = Path(self.directory.name, "partial")
        stream = mock.MagicMock()
        stream.__enter__.return_value.name = str(temporary)
        stream.__enter__.return_value.flush.side_effect = OSError("full")
        with (
            mock.patch.object(bindings.tempfile, "NamedTemporaryFile", return_value=stream),
            mock.patch.object(Path, "unlink") as unlink,
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be written"),
        ):
            self.store._write([binding])
        unlink.assert_called_once_with(missing_ok=True)

    def test_binding_decoder_rejects_outer_and_resolution_shapes(self) -> None:
        for value in (
            None,
            {
                "team_id": "team_1",
                "binding_digest": IMAGE_ID,
                "provenance": "published",
                "resolution": [],
            },
        ):
            with self.subTest(value=value), self.assertRaisesRegex(bindings.DynamicAssistantError, "malformed"):
                bindings._decode_binding(value)

    def test_reserved_assistant_identity_is_rejected_after_contract_validation(self) -> None:
        with (
            mock.patch.object(bindings._CONTRACTS, "validate"),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "reserved"),
        ):
            bindings.binding_from_resolution("team_1", {"assistant_id": "postgres"})

    def test_binding_lock_normalizes_partial_os_and_generic_failures(self) -> None:
        path = Path(self.directory.name, "lock")

        with (
            mock.patch.object(bindings.os, "open", return_value=9),
            mock.patch.object(bindings.os, "fdopen", side_effect=OSError("failed")),
            mock.patch.object(bindings.os, "close") as close,
            self.assertRaisesRegex(bindings.DynamicAssistantError, "lock is unavailable"),
        ):
            bindings._FileLock(path, 1).__enter__()
        close.assert_called_once_with(9)

        stream = mock.Mock()
        with (
            mock.patch.object(bindings.os, "open", return_value=9),
            mock.patch.object(bindings.os, "fdopen", return_value=stream),
            mock.patch.object(bindings.fcntl, "flock", side_effect=OSError("failed")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "lock is unavailable"),
        ):
            bindings._FileLock(path, 1).__enter__()
        stream.close.assert_called_once()

        with (
            mock.patch.object(bindings.os, "open", return_value=9),
            mock.patch.object(bindings.os, "fdopen", side_effect=ValueError("failed")),
            mock.patch.object(bindings.os, "close") as close,
            self.assertRaisesRegex(ValueError, "failed"),
        ):
            bindings._FileLock(path, 1).__enter__()
        close.assert_called_once_with(9)

        stream = mock.Mock()
        with (
            mock.patch.object(bindings.os, "open", return_value=9),
            mock.patch.object(bindings.os, "fdopen", return_value=stream),
            mock.patch.object(bindings.fcntl, "flock", side_effect=ValueError("failed")),
            self.assertRaisesRegex(ValueError, "failed"),
        ):
            bindings._FileLock(path, 1).__enter__()
        stream.close.assert_called_once()

        with self.assertRaisesRegex(bindings.DynamicAssistantError, "lock is unavailable"):
            bindings._FileLock(path, 1).__exit__()


class UpdateStoreEdgeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.binding_store = bindings.DynamicAssistantStore(root / "bindings.json")
        self.previous = self.binding_store.put("team_1", copy.deepcopy(RESOLUTION))
        self.updates = update.AssistantUpdateStore(root / "updates")
        self.residues = update.AssistantResidueStore(root / "residues")

    def test_begin_rejects_noop_identity_and_image_edges(self) -> None:
        with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "transaction is invalid"):
            self.updates.begin(self.previous, copy.deepcopy(RESOLUTION), IMAGE_ID)
        with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "image id is invalid"):
            self.updates.begin(self.previous, _successor(), "invalid")

    def test_update_listing_rejects_io_and_filename_drift(self) -> None:
        with (
            mock.patch.object(Path, "glob", side_effect=OSError("denied")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be listed"),
        ):
            self.updates.list()

        self.updates.begin(self.previous, _successor(), IMAGE_ID)
        path = next((Path(self.directory.name) / "updates").glob("*.json"))
        path.rename(path.with_name("wrong.json"))
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "filename is invalid"):
            self.updates.list()

        with (
            mock.patch.object(Path, "glob", return_value=(Path(self.directory.name, "vanished.json"),)),
            mock.patch.object(self.updates, "_read", return_value=None),
        ):
            self.assertEqual(self.updates.list(), ())

    def test_update_clear_rejects_changed_state_and_unlink_failure(self) -> None:
        current = self.updates.begin(self.previous, _successor(), IMAGE_ID)
        different = update.AssistantUpdate(
            current.team_id,
            current.assistant_id,
            current.previous,
            current.successor,
            "sha256:" + "b" * 64,
        )
        with (
            mock.patch.object(self.updates, "_read", return_value=different),
            self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "changed"),
        ):
            self.updates.clear(current)
        with (
            mock.patch.object(Path, "unlink", side_effect=OSError("read-only")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be cleared"),
        ):
            self.updates.clear(current)

    def test_update_reader_rejects_io_size_and_json_failures(self) -> None:
        path = Path(self.directory.name, "update.json")
        with (
            mock.patch.object(Path, "read_bytes", side_effect=OSError("denied")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be read"),
        ):
            self.updates._read(path)
        for raw in (b"", b"x" * (update._MAX_BYTES + 1), b"\xff", b"{"):
            with self.subTest(size=len(raw)):
                path.write_bytes(raw)
                with self.assertRaisesRegex(bindings.DynamicAssistantError, "malformed"):
                    self.updates._read(path)

    def test_residue_store_rejects_changed_list_clear_and_read_states(self) -> None:
        residue = self.residues.add(IMAGE_ID)
        with (
            mock.patch.object(self.residues, "_read", return_value=update.AssistantResidue("sha256:" + "b" * 64)),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "changed unexpectedly"),
        ):
            self.residues.add(IMAGE_ID)

        with (
            mock.patch.object(Path, "glob", side_effect=OSError("denied")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be listed"),
        ):
            self.residues.list()

        path = next((Path(self.directory.name) / "residues").glob("*.json"))
        wrong = path.with_name("wrong.json")
        path.rename(wrong)
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "filename is invalid"):
            self.residues.list()
        wrong.rename(path)

        with (
            mock.patch.object(Path, "glob", return_value=(Path(self.directory.name, "vanished.json"),)),
            mock.patch.object(self.residues, "_read", return_value=None),
        ):
            self.assertEqual(self.residues.list(), ())

        with (
            mock.patch.object(self.residues, "_read", return_value=update.AssistantResidue("sha256:" + "b" * 64)),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "changed unexpectedly"),
        ):
            self.residues.clear(residue)
        with (
            mock.patch.object(Path, "unlink", side_effect=OSError("read-only")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be cleared"),
        ):
            self.residues.clear(residue)

        with (
            mock.patch.object(Path, "read_bytes", side_effect=OSError("denied")),
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be read"),
        ):
            self.residues._read(path)
        for raw in (b"", b"x" * 1025, b"\xff", b"{", b"[]"):
            with self.subTest(size=len(raw)):
                path.write_bytes(raw)
                with self.assertRaisesRegex(bindings.DynamicAssistantError, "malformed"):
                    self.residues._read(path)

    def test_update_decode_rejects_shapes_and_cross_assistant_identity(self) -> None:
        for value in (None, {"version": 1}):
            with self.subTest(value=value), self.assertRaisesRegex(bindings.DynamicAssistantError, "malformed"):
                update._decode(value)

        current = self.updates.begin(self.previous, _successor(), IMAGE_ID)
        encoded = update._encode(current)
        encoded["version"] = 1
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "malformed"):
            update._decode(encoded)

        encoded = update._encode(current)
        encoded["assistant_id"] = "other"
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "malformed"):
            update._decode(encoded)

    def test_update_write_cleans_partial_file_and_invalid_residue_id_fails(self) -> None:
        path = Path(self.directory.name, "updates", "state.json")
        temporary = Path(self.directory.name, "partial")
        stream = mock.MagicMock()
        stream.__enter__.return_value.name = str(temporary)
        stream.__enter__.return_value.flush.side_effect = OSError("full")
        with (
            mock.patch.object(update.tempfile, "NamedTemporaryFile", return_value=stream),
            mock.patch.object(Path, "unlink") as unlink,
            self.assertRaisesRegex(bindings.DynamicAssistantError, "cannot be written"),
        ):
            update._write(path, {"value": True})
        unlink.assert_called_once_with(missing_ok=True)

        with self.assertRaisesRegex(bindings.DynamicAssistantError, "image id is invalid"):
            self.residues.add("invalid")

    def test_update_lock_normalizes_partial_os_and_generic_failures(self) -> None:
        path = Path(self.directory.name, "lock")
        with (
            mock.patch.object(update.os, "open", return_value=9),
            mock.patch.object(update.os, "fdopen", side_effect=OSError("failed")),
            mock.patch.object(update.os, "close") as close,
            self.assertRaisesRegex(bindings.DynamicAssistantError, "lock is unavailable"),
        ):
            update._FileLock(path, 1).__enter__()
        close.assert_called_once_with(9)

        for exception in (OSError("failed"), ValueError("failed")):
            stream = mock.Mock()
            expected = bindings.DynamicAssistantError if isinstance(exception, OSError) else ValueError
            with (
                self.subTest(exception=type(exception).__name__),
                mock.patch.object(update.os, "open", return_value=9),
                mock.patch.object(update.os, "fdopen", return_value=stream),
                mock.patch.object(update.fcntl, "flock", side_effect=exception),
                self.assertRaises(expected),
            ):
                update._FileLock(path, 1).__enter__()
            stream.close.assert_called_once()

        with (
            mock.patch.object(update.os, "open", return_value=9),
            mock.patch.object(update.os, "fdopen", side_effect=ValueError("failed")),
            mock.patch.object(update.os, "close") as close,
            self.assertRaisesRegex(ValueError, "failed"),
        ):
            update._FileLock(path, 1).__enter__()
        close.assert_called_once_with(9)

        with self.assertRaisesRegex(bindings.DynamicAssistantError, "lock is unavailable"):
            update._FileLock(path, 1).__exit__()


if __name__ == "__main__":
    unittest.main()
