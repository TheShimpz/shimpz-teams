from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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

            with self.assertRaisesRegex(stored_input.StoredInputStoreError, "authentication failed"):
                self._store(Path(directory)).metadata(
                    "team_2",
                    "other-assistant",
                    {
                        "other-token": {
                            "kind": "password",
                            "label": "Other token",
                            "description": "Token for another boundary.",
                        }
                    },
                )

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
            for value in ("", "x" * (stored_input.MAX_VALUE_CHARACTERS + 1), object()):
                with self.subTest(value_type=type(value).__name__), self.assertRaises(
                    stored_input.StoredInputValidationError
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


if __name__ == "__main__":
    unittest.main()
