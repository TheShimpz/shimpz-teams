from __future__ import annotations

import base64
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from local.chat import continuation_store as continuation_store


class _Unencodable(str):
    def strip(self, _chars=None):
        return self

    def isprintable(self) -> bool:
        return True

    def encode(self, _encoding="utf-8", _errors="strict"):
        raise UnicodeError("invalid")


class ContinuationStoreValidationEdgeTests(unittest.TestCase):
    def test_identifiers_and_bindings_reject_every_invalid_family(self) -> None:
        validators = (
            (continuation_store._team_id, None),
            (continuation_store._team_id, "INVALID"),
            (continuation_store._kind, None),
            (continuation_store._kind, "unknown"),
            (continuation_store._challenge_id, None),
            (continuation_store._challenge_id, "short"),
        )
        for validator, value in validators:
            with (
                self.subTest(validator=validator.__name__, value=value),
                self.assertRaises(continuation_store.ContinuationStoreError),
            ):
                validator(value)

        invalid_bindings = (
            "binding",
            b"binding",
            {"binding": True},
            [],
            ["duplicate", "duplicate"],
            [1],
            [""],
            [" padded "],
            ["not\nprintable"],
            [_Unencodable("binding")],
            ["x" * (continuation_store.MAX_BINDING_BYTES + 1)],
            [str(index) for index in range(continuation_store.MAX_BINDINGS + 1)],
        )
        for bindings in invalid_bindings:
            with self.subTest(bindings=bindings), self.assertRaises(continuation_store.ContinuationStoreError):
                continuation_store._bindings(bindings)

    def test_private_file_errors_and_size_limits_fail_closed(self) -> None:
        path = Path("/private")
        with (
            mock.patch.object(continuation_store.os, "open", side_effect=OSError("unavailable")),
            self.assertRaises(continuation_store.ContinuationStoreError),
        ):
            continuation_store._read_private_file(path, 10, "state")

        with (
            mock.patch.object(continuation_store.os, "open", return_value=7),
            mock.patch.object(
                continuation_store.os,
                "fstat",
                return_value=types.SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o600,
                    st_uid=os.geteuid(),
                    st_nlink=1,
                    st_size=0,
                ),
            ),
            mock.patch.object(continuation_store.os, "close"),
            self.assertRaisesRegex(
                continuation_store.ContinuationStoreError,
                "ownership contract",
            ),
        ):
            continuation_store._read_private_file(path, 10, "state")

        with (
            mock.patch.object(continuation_store.os, "open", return_value=7),
            mock.patch.object(
                continuation_store.os,
                "fstat",
                return_value=types.SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600,
                    st_uid=os.geteuid(),
                    st_nlink=1,
                    st_size=0,
                ),
            ),
            mock.patch.object(continuation_store.os, "read", return_value=b"xxx"),
            mock.patch.object(continuation_store.os, "close"),
            self.assertRaisesRegex(
                continuation_store.ContinuationStoreError,
                "fixed byte limit",
            ),
        ):
            continuation_store._read_private_file(path, 2, "state")

    def test_private_parent_and_atomic_write_errors_are_mapped(self) -> None:
        path = mock.Mock()
        path.mkdir.side_effect = OSError("unavailable")
        with self.assertRaisesRegex(
            continuation_store.ContinuationStoreError,
            "directory is unavailable",
        ):
            continuation_store._require_private_parent(path, "state")

        path = mock.Mock()
        path.stat.return_value = types.SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_uid=os.geteuid(),
        )
        with self.assertRaisesRegex(
            continuation_store.ContinuationStoreError,
            "ownership contract",
        ):
            continuation_store._require_private_parent(path, "state")

        target = Path("/state/continuations.json")
        with (
            mock.patch.object(continuation_store, "_require_private_parent"),
            mock.patch.object(continuation_store.os, "open", return_value=7),
            mock.patch.object(continuation_store.os, "write", return_value=0),
            mock.patch.object(continuation_store.os, "close") as close,
            self.assertRaisesRegex(
                continuation_store.ContinuationStoreError,
                "could not be persisted",
            ),
        ):
            continuation_store._atomic_write(target, b"payload", "state")
        close.assert_called_with(7)

    def test_envelope_parts_json_records_and_state_reject_malformed_shapes(self) -> None:
        invalid_parts = (
            (object(), {}),
            ("not-base64", {}),
            (base64.b64encode(b"short").decode("ascii"), {"expected": 12}),
            (base64.b64encode(b"").decode("ascii"), {"minimum": 1}),
            (base64.b64encode(b"long").decode("ascii"), {"maximum": 1}),
        )
        for value, options in invalid_parts:
            with (
                self.subTest(value=value, options=options),
                self.assertRaises(continuation_store.ContinuationStoreError),
            ):
                continuation_store._decode_part(value, **options)

        with self.assertRaises(continuation_store.ContinuationStoreError):
            continuation_store._record({}, "team_1")

        malformed = {
            "team_id": "team_1",
            "kind": "human",
            "challenge_id": "a" * 32,
            "expires_at": 1,
            "generation": 1,
            "bindings": ["binding"],
            "envelope": {
                "algorithm": "wrong",
                "nonce": base64.b64encode(b"n" * 12).decode("ascii"),
                "ciphertext": base64.b64encode(b"c" * 17).decode("ascii"),
            },
        }
        with self.assertRaises(continuation_store.ContinuationStoreError):
            continuation_store._record(malformed, "team_1")

        with self.assertRaises(continuation_store.ContinuationStoreError):
            continuation_store._decode_json(b"\xff")
        with self.assertRaises(continuation_store.ContinuationStoreError):
            continuation_store._state({})


class EncryptedContinuationStoreEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.state_path = root / "state" / "continuations.json"
        self.key_path = root / "key" / "aes256.key"

    def store(self, **options) -> continuation_store.EncryptedContinuationStore:
        return continuation_store.EncryptedContinuationStore(
            self.state_path,
            self.key_path,
            now=options.pop("now", lambda: 1_000),
            **options,
        )

    def test_constructor_maps_resolution_and_configuration_errors(self) -> None:
        with (
            mock.patch.object(Path, "resolve", side_effect=OSError("unavailable")),
            self.assertRaises(continuation_store.ContinuationStoreError),
        ):
            self.store()

        for now, capacity in ((None, 1), (lambda: 1, True), (lambda: 1, 0)):
            with self.subTest(now=now, capacity=capacity), self.assertRaises(ValueError):
                continuation_store.EncryptedContinuationStore(
                    self.state_path,
                    self.key_path,
                    now=now,
                    capacity=capacity,
                )

    def test_write_and_key_contracts_enforce_fixed_limits(self) -> None:
        store = self.store()
        with (
            mock.patch.object(continuation_store, "MAX_STATE_BYTES", 1),
            self.assertRaisesRegex(
                continuation_store.ContinuationStoreError,
                "fixed byte limit",
            ),
        ):
            store._write_state(continuation_store._empty_state())

        with self.assertRaisesRegex(
            continuation_store.ContinuationStoreError,
            "keyring is unavailable",
        ):
            store._key()

        self.key_path.parent.mkdir(mode=0o700)
        self.key_path.write_bytes(b"short")
        self.key_path.chmod(0o600)
        with self.assertRaisesRegex(
            continuation_store.ContinuationStoreError,
            "keyring is invalid",
        ):
            store._key()

    def test_put_rejects_payload_state_and_capacity_contract_drift(self) -> None:
        store = self.store()
        invalid_payloads = (
            (1_000, ("binding",), b"payload"),
            (1_901, ("binding",), b"payload"),
            (1_100, ("binding",), "payload"),
            (1_100, ("binding",), b""),
        )
        for expires_at, bindings, payload in invalid_payloads:
            with (
                self.subTest(expires_at=expires_at, payload=payload),
                self.assertRaises(continuation_store.ContinuationStoreError),
            ):
                store.put(
                    "team_1",
                    "human",
                    "a" * 32,
                    expires_at,
                    bindings,
                    payload,
                )

        store._read_state = lambda: {"schema": 1, "records": []}
        with self.assertRaisesRegex(
            continuation_store.ContinuationStoreError,
            "state is malformed",
        ):
            store.put(
                "team_1",
                "human",
                "a" * 32,
                1_100,
                ("binding",),
                b"payload",
            )

    def test_resolved_rejects_impossible_envelope_and_plaintext_drift(self) -> None:
        store = self.store()
        record = {
            "kind": "human",
            "challenge_id": "a" * 32,
            "expires_at": 1_100,
            "generation": 1,
            "bindings": ["binding"],
            "envelope": object(),
        }
        with (
            mock.patch.object(continuation_store, "_record", return_value=record),
            self.assertRaisesRegex(
                continuation_store.ContinuationStoreError,
                "envelope is malformed",
            ),
        ):
            store._resolved("team_1", object())

        record["envelope"] = {
            "nonce": base64.b64encode(b"n" * 12).decode("ascii"),
            "ciphertext": base64.b64encode(b"c" * 17).decode("ascii"),
        }
        cipher = mock.Mock()
        cipher.decrypt.return_value = b""
        with (
            mock.patch.object(continuation_store, "_record", return_value=record),
            mock.patch.object(continuation_store, "AESGCM", return_value=cipher),
            mock.patch.object(store, "_key", return_value=b"k" * 32),
            self.assertRaisesRegex(
                continuation_store.ContinuationStoreError,
                "decrypted continuation is malformed",
            ),
        ):
            store._resolved("team_1", object())

    def test_collection_operations_reject_non_mapping_state_and_clear_both_paths(self) -> None:
        store = self.store()
        for operation in (store.active, store.drain_expired, lambda: store.delete("team_1"), store.clear):
            store._read_state = lambda: {"schema": 1, "records": []}
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(
                    continuation_store.ContinuationStoreError,
                    "state is malformed",
                ),
            ):
                operation()

        store = self.store()
        self.assertEqual(store.clear(), 0)
        store.put(
            "team_1",
            "human",
            "a" * 32,
            1_100,
            ("binding",),
            b"payload",
        )
        self.assertEqual(store.clear(), 1)
        self.assertEqual(store.active(), ())


if __name__ == "__main__":
    unittest.main()
