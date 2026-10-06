from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from local.chat import continuation_store as local_chat_continuation_store


class EncryptedContinuationStoreTests(unittest.TestCase):
    @staticmethod
    def _paths(directory: str) -> tuple[Path, Path]:
        root = Path(directory)
        return (
            root / "continuations" / "state" / "continuations.json",
            root / "continuations" / "key" / "aes256.key",
        )

    def test_sealed_state_bytes_stay_pinned_and_decode_after_reopen(self) -> None:
        # Existing Local state holds exactly these bytes; a format change must be a deliberate contract change.
        with tempfile.TemporaryDirectory() as directory:
            state_path, key_path = self._paths(directory)
            key_path.parent.mkdir(mode=0o700, parents=True)
            key_path.write_bytes(bytes(range(32)))
            key_path.chmod(0o600)
            store = local_chat_continuation_store.EncryptedContinuationStore(state_path, key_path, now=lambda: 1_000)
            nonces = iter((b"\x01" * 12, b"\x02" * 12))
            with mock.patch("os.urandom", side_effect=lambda _size: next(nonces)):
                store.put("team_1", "integrations", "a" * 32, 1_300, ("b-binding", "a-binding ✓"), b"\x00private\xff")
                store.put("team_2", "human", "b" * 32, 1_400, ("one",), b"second")

            self.assertEqual(
                hashlib.sha256(state_path.read_bytes()).hexdigest(),
                "f89fd62bcd43e245bd42ba333849021e3f22d003e940699ac32796e37efae811",
            )
            reopened = local_chat_continuation_store.EncryptedContinuationStore(state_path, key_path, now=lambda: 1_001)
            first = reopened.current("team_1")
            self.assertEqual((first.bindings, first.payload), (("a-binding ✓", "b-binding"), b"\x00private\xff"))

    def test_round_trip_survives_reopen_without_plaintext_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path, key_path = self._paths(directory)
            payload = b'{"integration":"private integration state","turn":"paused"}'
            store = local_chat_continuation_store.EncryptedContinuationStore(
                state_path,
                key_path,
                now=lambda: 1_000,
            )
            saved = store.put(
                "team_1",
                "integrations",
                "a" * 32,
                1_300,
                ("assistant/action/image@sha256:" + "b" * 64 + "/0",),
                payload,
            )
            reopened = local_chat_continuation_store.EncryptedContinuationStore(
                state_path,
                key_path,
                now=lambda: 1_001,
            )

            self.assertEqual(reopened.current("team_1"), saved)
            self.assertNotIn(b"private integration state", state_path.read_bytes())
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(key_path.parent.stat().st_mode), 0o700)

    def test_generation_and_aad_bind_every_routing_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path, key_path = self._paths(directory)
            store = local_chat_continuation_store.EncryptedContinuationStore(
                state_path,
                key_path,
                now=lambda: 2_000,
            )
            first = store.put(
                "team_1",
                "integrations",
                "b" * 32,
                2_300,
                ("assistant/action/release/0",),
                b'{"integration":"connected"}',
            )
            second = store.put(
                "team_1",
                "integrations",
                "c" * 32,
                2_300,
                ("assistant/action/release/1",),
                b'{"integration":"connected"}',
            )
            self.assertEqual((first.generation, second.generation), (1, 2))

            state = json.loads(state_path.read_text(encoding="ascii"))
            state["records"]["team_1"]["challenge_id"] = "d" * 32
            state_path.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":")),
                encoding="ascii",
            )
            state_path.chmod(0o600)
            with self.assertRaisesRegex(
                local_chat_continuation_store.ContinuationStoreError,
                "authentication failed",
            ):
                store.current("team_1")

    def test_expiry_and_exact_delete_are_fail_closed(self) -> None:
        clock = [3_000]
        with tempfile.TemporaryDirectory() as directory:
            state_path, key_path = self._paths(directory)
            store = local_chat_continuation_store.EncryptedContinuationStore(
                state_path,
                key_path,
                now=lambda: clock[0],
            )
            store.put(
                "team_1",
                "integrations",
                "e" * 32,
                3_001,
                ("assistant/action/release/0",),
                b"{}",
            )
            with self.assertRaises(local_chat_continuation_store.ContinuationNotFoundError):
                store.delete("team_1", "f" * 32)
            clock[0] = 3_001
            self.assertIsNone(store.current("team_1"))
            self.assertFalse(store.delete("team_1"))

    def test_expired_continuation_is_drained_once_with_its_decrypted_payload(self) -> None:
        clock = [3_000]
        with tempfile.TemporaryDirectory() as directory:
            state_path, key_path = self._paths(directory)
            store = local_chat_continuation_store.EncryptedContinuationStore(
                state_path,
                key_path,
                now=lambda: clock[0],
            )
            saved = store.put(
                "team_1",
                "human",
                "e" * 32,
                3_001,
                ("assistant/action/release/0",),
                b'{"pending":"encrypted"}',
            )

            clock[0] = 3_001

            self.assertEqual(store.drain_expired(), (saved,))
            self.assertEqual(store.drain_expired(), ())
            self.assertEqual(store.active(), ())

    def test_rejects_unsafe_paths_capacity_and_oversized_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path, key_path = self._paths(directory)
            with self.assertRaises(local_chat_continuation_store.ContinuationStoreError):
                local_chat_continuation_store.EncryptedContinuationStore(
                    Path("relative-state"),
                    key_path,
                )
            with self.assertRaises(local_chat_continuation_store.ContinuationStoreError):
                local_chat_continuation_store.EncryptedContinuationStore(
                    state_path,
                    state_path.with_name("key"),
                )
            store = local_chat_continuation_store.EncryptedContinuationStore(
                state_path,
                key_path,
                now=lambda: 4_000,
                capacity=1,
            )
            store.put(
                "team_1",
                "integrations",
                "1" * 32,
                4_300,
                ("assistant/action/release/0",),
                b"{}",
            )
            with self.assertRaisesRegex(
                local_chat_continuation_store.ContinuationStoreError,
                "capacity",
            ):
                store.put(
                    "team_2",
                    "integrations",
                    "2" * 32,
                    4_300,
                    ("assistant/action/release/0",),
                    b"{}",
                )
            with self.assertRaises(local_chat_continuation_store.ContinuationStoreError):
                store.put(
                    "team_1",
                    "input",
                    "3" * 32,
                    4_300,
                    ("assistant/action/release/0",),
                    b"x" * (local_chat_continuation_store.MAX_PLAINTEXT_BYTES + 1),
                )


if __name__ == "__main__":
    unittest.main()
