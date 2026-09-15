from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from action import stored_input

TOKEN = "whatsapp-token-private-material-123456789"
ORIGIN = "a" * 64
DECLARATIONS = {
    "whatsapp-token": SimpleNamespace(
        kind="password",
        label="WhatsApp token",
        description="Token used to call the WhatsApp API.",
    )
}


class StoredInputStoreTests(unittest.TestCase):
    @staticmethod
    def _store(root: Path) -> stored_input.StoredInputStore:
        return stored_input.StoredInputStore(
            root / "state" / "stored-inputs.json",
            root / "key" / "aes256.key",
        )

    def test_inventory_seals_encrypted_value_and_never_projects_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)

            self.assertEqual(
                store.metadata("team_1", "whatsapp", DECLARATIONS),
                (
                    stored_input.StoredInputMetadata(
                        "whatsapp-token",
                        "password",
                        "WhatsApp token",
                        "Token used to call the WhatsApp API.",
                        "missing",
                        0,
                    ),
                ),
            )
            self.assertEqual(store.seal("team_1", "whatsapp", "whatsapp-token", "password", TOKEN, ORIGIN), 1)
            resolved = store.resolve("team_1", "whatsapp", "whatsapp-token", "password")

            self.assertEqual((resolved.value, resolved.generation, resolved.origin), (TOKEN, 1, ORIGIN))
            self.assertNotIn(TOKEN, repr(resolved))
            metadata = store.metadata("team_1", "whatsapp", DECLARATIONS)[0]
            self.assertEqual((metadata.status, metadata.generation), ("stored", 1))
            encoded_state = store.state_path.read_bytes()
            key = store.key_path.read_bytes()
            self.assertNotIn(TOKEN.encode(), encoded_state)
            self.assertNotIn(TOKEN.encode(), key)
            self.assertNotIn(b'"value"', encoded_state)
            self.assertEqual(stat.S_IMODE(store.state_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.key_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.state_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.key_path.parent.stat().st_mode), 0o700)
            inventory = store.inventory(
                "team_1",
                (SimpleNamespace(assistant_id="whatsapp", stored_inputs=DECLARATIONS),),
            )
            self.assertEqual(
                inventory,
                {
                    "team_id": "team_1",
                    "stored_inputs": [
                        {
                            "assistant_id": "whatsapp",
                            "stored_input_id": "whatsapp-token",
                            "status": "stored",
                        }
                    ],
                },
            )
            self.assertNotIn(TOKEN, repr(inventory))

    def test_rotation_and_external_atomic_replacement_refresh_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._store(root)
            second = self._store(root)
            self.assertEqual(first.seal("team_1", "whatsapp", "whatsapp-token", "password", TOKEN, ORIGIN), 1)
            self.assertEqual(
                first.resolve("team_1", "whatsapp", "whatsapp-token", "password").value,
                TOKEN,
            )

            replacement = "replacement-whatsapp-token-private-material"
            self.assertEqual(
                second.seal("team_1", "whatsapp", "whatsapp-token", "password", replacement, "b" * 64),
                2,
            )

            refreshed = first.resolve("team_1", "whatsapp", "whatsapp-token", "password")
            self.assertEqual((refreshed.value, refreshed.generation, refreshed.origin), (replacement, 2, "b" * 64))

    def test_aad_prevents_cross_team_assistant_or_identifier_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.seal("team_1", "whatsapp", "whatsapp-token", "password", TOKEN, ORIGIN)
            state = json.loads(store.state_path.read_bytes())
            record = state["teams"]["team_1"]["whatsapp"]["whatsapp-token"]
            state["teams"] = {"team_2": {"other-assistant": {"other-token": record}}}
            store.state_path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")

            moved = self._store(Path(directory))
            declarations = {
                "other-token": {
                    "kind": "password",
                    "label": "Other token",
                    "description": "Token for another boundary.",
                }
            }
            self.assertEqual(moved.metadata("team_2", "other-assistant", declarations)[0].status, "stored")
            with self.assertRaisesRegex(stored_input.StoredInputStoreError, "authentication failed"):
                moved.resolve("team_2", "other-assistant", "other-token", "password")
            self.assertTrue(moved.delete("team_2", "other-assistant", "other-token"))
            self.assertEqual(moved.metadata("team_2", "other-assistant", declarations)[0].status, "missing")

    def test_team_isolation_and_exact_lifecycle_cleanup_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.seal("team_1", "whatsapp", "whatsapp-token", "password", TOKEN, ORIGIN)
            store.seal("team_1", "other", "other-token", "password", "other-assistant-token", "b" * 64)
            store.seal("team_2", "whatsapp", "whatsapp-token", "password", "other-team-token", "c" * 64)

            self.assertTrue(store.retain_declared("team_1", "whatsapp", ()))
            self.assertFalse(store.retain_declared("team_1", "whatsapp", ()))
            self.assertEqual(
                store.resolve("team_1", "other", "other-token", "password").value,
                "other-assistant-token",
            )
            self.assertEqual(
                store.resolve("team_2", "whatsapp", "whatsapp-token", "password").value,
                "other-team-token",
            )
            self.assertTrue(store.delete_team("team_1"))
            self.assertFalse(store.delete_team("team_1"))
            self.assertTrue(store.delete_all())
            self.assertFalse(store.delete_all())

    def test_exact_delete_and_missing_resolution_do_not_create_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            with self.assertRaises(stored_input.StoredInputMissingError):
                store.resolve("team_1", "whatsapp", "whatsapp-token", "password")
            self.assertFalse(store.delete("team_1", "whatsapp", "whatsapp-token"))
            self.assertFalse(store.delete_assistant("team_1", "whatsapp"))
            self.assertFalse(store.delete_team("team_1"))
            self.assertFalse(store.delete_all())
            self.assertFalse(store.state_path.exists())
            self.assertFalse(store.key_path.exists())

    def test_paths_declarations_values_and_corrupt_state_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(stored_input.StoredInputStoreError):
                stored_input.StoredInputStore(Path("relative.json"), root / "key")
            with self.assertRaises(stored_input.StoredInputStoreError):
                stored_input.StoredInputStore(root / "same" / "state", root / "same" / "key")

            store = self._store(root)
            with self.assertRaises(stored_input.StoredInputValidationError):
                store.seal("team_1", "whatsapp", "whatsapp-token", "text", TOKEN, ORIGIN)
            with self.assertRaises(stored_input.StoredInputValidationError):
                store.resolve("team_1", "whatsapp", "whatsapp-token", "text")
            for value in ("", "x" * (stored_input.MAX_VALUE_CHARACTERS + 1), object()):
                with (
                    self.subTest(value_type=type(value).__name__),
                    self.assertRaises(stored_input.StoredInputValidationError),
                ):
                    store.seal("team_1", "whatsapp", "whatsapp-token", "password", value, ORIGIN)
            with self.assertRaises(stored_input.StoredInputValidationError):
                store.seal("team_1", "whatsapp", "whatsapp-token", "password", TOKEN, "not-an-origin")
            with self.assertRaises(stored_input.StoredInputValidationError):
                store.metadata("team_1", "whatsapp", {"WhatsApp_Token": DECLARATIONS["whatsapp-token"]})

            store.state_path.parent.mkdir(mode=0o700)
            store.state_path.write_text("{", encoding="ascii")
            store.state_path.chmod(0o600)
            with self.assertRaises(stored_input.StoredInputStoreError):
                store.metadata("team_1", "whatsapp", DECLARATIONS)

    def test_identifier_kind_and_public_declaration_validation_fail_closed(self) -> None:
        invalid_calls = (
            lambda: stored_input._team_id("Team"),
            lambda: stored_input._component_id("WhatsApp_Token", "Stored Input id"),
            lambda: stored_input._kind("text"),
            lambda: stored_input._public_text("", "label", 80),
            lambda: stored_input._public_text(" padded ", "label", 80),
            lambda: stored_input._public_text("x" * 81, "label", 80),
            lambda: stored_input._public_text("line\nfeed", "label", 80),
            lambda: stored_input._declarations(object()),
            lambda: stored_input._declarations({"token": object()}),
            lambda: stored_input._declared_ids("token"),
            lambda: stored_input._declared_ids(("token", "token")),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(stored_input.StoredInputValidationError):
                call()

        too_many = {
            f"token-{index}": {"kind": "password", "label": "Token", "description": "Secret"}
            for index in range(stored_input.MAX_STORED_INPUTS_PER_ASSISTANT + 1)
        }
        with self.assertRaises(stored_input.StoredInputValidationError):
            stored_input._declarations(too_many)
        with self.assertRaises(stored_input.StoredInputValidationError):
            stored_input._declared_ids(too_many)
        self.assertEqual(stored_input._declared_ids({"token": object()}), ("token",))

    def test_state_shape_and_record_metadata_fail_closed(self) -> None:
        valid_record = {
            "kind": "password",
            "generation": 1,
            "updated_at": "2026-09-15T12:00:00Z",
            "envelope": {
                "algorithm": "AES-256-GCM",
                "nonce": "AAAAAAAAAAAAAAAA",
                "ciphertext": "AAAAAAAAAAAAAAAAAAAAAAA=",
            },
        }
        malformed_records = (
            None,
            {key: value for key, value in valid_record.items() if key != "kind"},
            {**valid_record, "kind": "text"},
            {**valid_record, "generation": True},
            {**valid_record, "updated_at": "now"},
            {**valid_record, "envelope": []},
            {**valid_record, "envelope": {"algorithm": "AES-256-GCM"}},
            {**valid_record, "envelope": {**valid_record["envelope"], "algorithm": "AES-128-GCM"}},
            {**valid_record, "envelope": {**valid_record["envelope"], "nonce": "invalid"}},
            {**valid_record, "envelope": {**valid_record["envelope"], "ciphertext": "invalid"}},
        )
        for record in malformed_records:
            with self.subTest(record=record), self.assertRaises(stored_input.StoredInputStoreError):
                stored_input._validate_record(record)

        malformed_states = (
            None,
            {},
            {"schema": 2, "teams": {}},
            {"schema": 1, "teams": []},
        )
        for state in malformed_states:
            with self.subTest(state=state), self.assertRaises(stored_input.StoredInputStoreError):
                stored_input._validate_state(state)

        malformed_assistants = (
            ("Team", {}),
            ("team_1", []),
            ("team_1", {"Bad": {}}),
            ("team_1", {"whatsapp": []}),
            ("team_1", {"whatsapp": {"Bad": valid_record}}),
        )
        for team, assistants in malformed_assistants:
            with self.subTest(team=team, assistants=assistants), self.assertRaises(
                stored_input.StoredInputStoreError
            ):
                stored_input._validate_assistants(team, assistants)

        with mock.patch.object(stored_input, "MAX_STORED_INPUTS_PER_ASSISTANT", 0), self.assertRaises(
            stored_input.StoredInputStoreError
        ):
            stored_input._validate_assistants("team_1", {"whatsapp": {"token": valid_record}})
        with mock.patch.object(stored_input, "MAX_TOTAL_RECORDS", 0), self.assertRaisesRegex(
            stored_input.StoredInputStoreError,
            "record limit",
        ):
            stored_input._validate_state(
                {"schema": 1, "teams": {"team_1": {"whatsapp": {"token": valid_record}}}}
            )

    def test_storage_operational_limits_and_cache_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(stored_input.Path, "resolve", side_effect=OSError("offline")),
                self.assertRaisesRegex(stored_input.StoredInputStoreError, "paths are unavailable"),
            ):
                self._store(root)

            store = self._store(root)
            unchanged = SimpleNamespace(unchanged=True, identity=None, payload=None)
            with mock.patch.object(
                stored_input.private_state.PrivateState,
                "read_private_file_if_changed",
                return_value=unchanged,
            ), self.assertRaisesRegex(stored_input.StoredInputStoreError, "cache is unavailable"):
                store._read_state()

            with mock.patch.object(stored_input, "MAX_STATE_BYTES", 1), self.assertRaisesRegex(
                stored_input.StoredInputStoreError,
                "byte limit",
            ):
                store._write_state(stored_input.private_state.empty_state())
            with mock.patch.object(stored_input, "MAX_PLAINTEXT_BYTES", 1), self.assertRaises(
                stored_input.StoredInputValidationError
            ):
                store._plaintext("secret", ORIGIN)
            with mock.patch.object(stored_input, "MAX_VALUE_BYTES", 1), self.assertRaises(
                stored_input.StoredInputValidationError
            ):
                stored_input._secret_value("secret")

            store.seal("team_1", "whatsapp", "token-one", "password", TOKEN, ORIGIN)
            with mock.patch.object(stored_input, "MAX_STORED_INPUTS_PER_ASSISTANT", 1), self.assertRaisesRegex(
                stored_input.StoredInputStoreError,
                "capacity reached",
            ):
                store.seal("team_1", "whatsapp", "token-two", "password", TOKEN, ORIGIN)

    def test_decrypted_values_inventory_and_assistant_cleanup_fail_closed(self) -> None:
        malformed_plaintexts = (
            b"x" * (stored_input.MAX_PLAINTEXT_BYTES + 1),
            b"[]",
            b'{"origin":"bad","value":"secret"}',
        )
        for plaintext in malformed_plaintexts:
            with self.subTest(plaintext=plaintext[:20]), self.assertRaises(stored_input.StoredInputStoreError):
                stored_input.StoredInputStore._decrypted_value(plaintext)

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.seal("team_1", "whatsapp", "whatsapp-token", "password", TOKEN, ORIGIN)
            duplicate = SimpleNamespace(assistant_id="whatsapp", stored_inputs=DECLARATIONS)
            with self.assertRaisesRegex(stored_input.StoredInputValidationError, "ambiguous"):
                store.inventory("team_1", (duplicate, duplicate))
            self.assertTrue(store.delete_assistant("team_1", "whatsapp"))
            self.assertFalse(store.delete_assistant("team_1", "whatsapp"))


if __name__ == "__main__":
    unittest.main()
