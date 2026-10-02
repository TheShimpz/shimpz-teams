"""Encrypted per-execution Routine diagnostics: AAD, incarnation, retention, bounds, and isolation (ADR-0092)."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local.errors import ApiProblemError
from local.routine import diagnostics
from protocol.http.v1 import routine as http_routine

NOW = 2_200_000_000
INCARNATION = "a" * 64
OTHER_INCARNATION = "b" * 64
ROUTINE = "c" * 32
RUN = "d" * 32
OPERATION = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"
FAILURE = {
    "error_type": "httpx.HTTPStatusError",
    "message": "Client error '404 Not Found'",
    "provider": "api.cloudflare.com",
    "http_status": 404,
    "response_excerpt": '{"success":false}',
    "redacted": False,
    "truncated": False,
}


def _diagnostic(**changes: object) -> diagnostics.Diagnostic:
    value = diagnostics.Diagnostic(
        routine_id=ROUTINE,
        run_id=RUN,
        operation_id=OPERATION,
        attempt=1,
        assistant_id="shimpz-cloudflare",
        action="replace-dns-record",
        recorded_at=NOW,
        failure=FAILURE,
    )
    return dataclasses.replace(value, **changes)


class DiagnosticStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = diagnostics.DiagnosticStore(self.root / "bodies", self.root / "key" / "aes256.key")

    def files(self, team_id: str = "team_1") -> list[Path]:
        directory = self.store._team_dir(team_id)
        return sorted(directory.iterdir()) if directory.exists() else []

    def test_a_sealed_body_reads_back_only_for_its_exact_run_in_order(self) -> None:
        later = _diagnostic(attempt=2, recorded_at=NOW + 5, failure=None, condition="exit-status:1")
        other_run = _diagnostic(run_id="e" * 32)
        for item in (later, _diagnostic(), other_run):
            self.store.record("team_1", INCARNATION, item, ("never-stored-secret",))
        read = self.store.read("team_1", INCARNATION, RUN, NOW + 10)
        self.assertEqual(read, (_diagnostic(), later))
        view = http_routine.canonical_diagnostics(
            {"team_id": "team_1", "run_id": RUN, "diagnostics": [item.view() for item in read]}
        )
        self.assertEqual(view["diagnostics"][1]["condition"], "exit-status:1")
        self.assertEqual(view["diagnostics"][0]["recorded_at"], "2039-09-18T23:06:40Z")
        for path in self.files():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            raw = path.read_bytes()
            self.assertLessEqual(len(raw), diagnostics.MAX_FILE_BYTES)
            self.assertNotIn(b"httpx", raw)
            self.assertNotIn(b"cloudflare", raw)
        self.assertNotIn(b"routine", self.store.key_path.read_bytes())

    def test_aad_binds_team_incarnation_routine_run_operation_attempt_and_instant(self) -> None:
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        self.assertEqual(self.store.read("team_1", OTHER_INCARNATION, RUN, NOW), ())
        [sealed] = self.files()
        moved = self.store._team_dir("team_2")
        moved.mkdir(mode=0o700)
        (moved / sealed.name).write_bytes(sealed.read_bytes())
        (moved / sealed.name).chmod(0o600)
        self.assertEqual(self.store.read("team_2", INCARNATION, RUN, NOW), ())
        for renamed in (
            sealed.name.replace(".1.diagnostic", ".2.diagnostic"),
            sealed.name.replace(OPERATION, "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7"),
            sealed.name.replace(ROUTINE, "f" * 32),
            sealed.name.replace(str(NOW), str(NOW + 1)),
        ):
            with self.subTest(renamed=renamed):
                target = sealed.with_name(renamed)
                target.write_bytes(sealed.read_bytes())
                target.chmod(0o600)
                self.assertEqual(self.store.read("team_1", INCARNATION, RUN, NOW + 2), (_diagnostic(),))
                target.unlink()

    def test_a_tampered_or_malformed_body_never_reads_as_evidence(self) -> None:
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        [sealed] = self.files()
        envelope = json.loads(sealed.read_bytes())
        ciphertext = bytearray(diagnostics.base64.b64decode(envelope["ciphertext"]))
        ciphertext[0] ^= 1
        envelope["ciphertext"] = diagnostics.base64.b64encode(bytes(ciphertext)).decode()
        sealed.write_text(json.dumps(envelope))
        self.assertEqual(self.store.read("team_1", INCARNATION, RUN, NOW), ())
        for malformed in (b"not json", json.dumps({"algorithm": "AES-128-GCM"}).encode()):
            with self.subTest(malformed=malformed), self.assertRaises(diagnostics.DiagnosticStoreError):
                sealed.write_bytes(malformed)
                self.store.read("team_1", INCARNATION, RUN, NOW)
        key = diagnostics._PRIVATE.key(self.store.key_path, "key")
        for payload in (b"not json", json.dumps({"x": 1}).encode()):
            with self.subTest(payload=payload), self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "malformed"):
                nonce = os.urandom(12)
                sealed.write_text(
                    json.dumps(
                        {
                            "algorithm": "AES-256-GCM",
                            "nonce": diagnostics.base64.b64encode(nonce).decode(),
                            "ciphertext": diagnostics.base64.b64encode(
                                diagnostics.AESGCM(key).encrypt(
                                    nonce, payload, diagnostics._aad("team_1", INCARNATION, sealed.name)
                                )
                            ).decode(),
                        }
                    )
                )
                self.store.read("team_1", INCARNATION, RUN, NOW)
        sealed.unlink()
        sealed.mkdir()
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "ownership"):
            self.store.read("team_1", INCARNATION, RUN, NOW)

    def test_bodies_expire_after_seven_days_and_the_same_attempt_replaces_its_body(self) -> None:
        old = _diagnostic(operation_id="7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7", recorded_at=NOW)
        self.store.record("team_1", INCARNATION, old, ())
        self.store.record("team_1", INCARNATION, _diagnostic(recorded_at=NOW + 1), ())
        self.store.record(
            "team_1", INCARNATION, _diagnostic(recorded_at=NOW + 2, condition="timeout", failure=None), ()
        )
        self.assertEqual(len(self.files()), 2)
        expired = NOW + diagnostics.RETENTION_SECONDS
        self.assertEqual([item.condition for item in self.store.read("team_1", INCARNATION, RUN, expired)], ["timeout"])
        self.store.record("team_1", INCARNATION, _diagnostic(attempt=2, recorded_at=expired + 2), ())
        self.assertEqual(len(self.files()), 1)

    def test_a_team_keeps_its_newest_bodies_within_its_byte_bound(self) -> None:
        for index in range(4):
            self.store.record("team_1", INCARNATION, _diagnostic(attempt=index + 1, recorded_at=NOW + index), ())
        size = self.files()[0].stat().st_size
        self.store.record("team_2", INCARNATION, _diagnostic(), ())
        with mock.patch.object(diagnostics, "MAX_TEAM_BYTES", size * 2 + 1):
            self.store.record("team_1", INCARNATION, _diagnostic(attempt=5, recorded_at=NOW + 9), ())
        self.assertEqual([item.attempt for item in self.store.read("team_1", INCARNATION, RUN, NOW + 9)], [4, 5])
        self.assertEqual(len(self.files("team_2")), 1)
        with mock.patch.object(http_routine, "MAX_RUN_DIAGNOSTICS", 1):
            self.assertEqual([item.attempt for item in self.store.read("team_1", INCARNATION, RUN, NOW + 9)], [5])

    def test_only_a_bounded_secret_free_diagnostic_is_ever_sealed(self) -> None:
        refused = (
            ("team_1", INCARNATION, _diagnostic(failure=None), "invalid"),
            ("team_1", INCARNATION, _diagnostic(routine_id="x"), "invalid"),
            ("team_1", INCARNATION, _diagnostic(run_id="x"), "invalid"),
            ("team_1", "A" * 64, _diagnostic(), "incarnation"),
        )
        for team_id, incarnation, item, message in refused:
            with self.subTest(message=message), self.assertRaisesRegex(diagnostics.DiagnosticStoreError, message):
                self.store.record(team_id, incarnation, item, ())
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "protected value"):
            self.store.record("team_1", INCARNATION, _diagnostic(), ("", "404 Not Found"))
        with (
            mock.patch.object(diagnostics, "MAX_PLAINTEXT_BYTES", 64),
            self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "byte limit"),
        ):
            self.store.record("team_1", INCARNATION, _diagnostic(), ())
        self.assertEqual(self.files(), [])
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "run is invalid"):
            self.store.read("team_1", INCARNATION, "x", NOW)
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "keyring"):
            diagnostics.DiagnosticStore(self.root / "bodies", self.root / "bodies" / "aes256.key")

    def test_deleting_a_routine_a_team_or_the_space_removes_exactly_their_bodies(self) -> None:
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        self.store.record("team_1", INCARNATION, _diagnostic(routine_id="f" * 32, run_id="e" * 32), ())
        self.store.record("team_2", INCARNATION, _diagnostic(), ())
        self.store.delete_routine("team_1", ROUTINE)
        self.assertEqual([path.name.split(".")[1] for path in self.files()], ["f" * 32])
        self.store.delete("team_1")
        self.store.delete("team_1")
        self.assertFalse(self.store._team_dir("team_1").exists())
        self.assertEqual(len(self.files("team_2")), 1)
        (self.store.root / "unrelated").write_text("kept")
        self.store.delete_all()
        self.store.delete_all()
        self.assertFalse(self.store.key_path.exists())
        self.assertEqual([path.name for path in self.store.root.iterdir()], ["unrelated"])
        diagnostics.DiagnosticStore(self.root / "absent", self.root / "absent-key" / "k").delete_all()

    def test_storage_failures_fail_closed(self) -> None:
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        [sealed] = self.files()
        (sealed.parent / "unrelated.tmp").write_text("ignored")
        self.assertEqual(len(self.store.read("team_1", INCARNATION, RUN, NOW)), 1)
        (sealed.parent / "unrelated.tmp").unlink()
        with mock.patch.object(diagnostics.private_state.PrivateState, "read_private_file", return_value=None):
            self.assertEqual(self.store.read("team_1", INCARNATION, RUN, NOW), ())
        with (
            mock.patch.object(diagnostics.os, "scandir", side_effect=PermissionError("denied")),
            self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "listed"),
        ):
            self.store.read("team_1", INCARNATION, RUN, NOW)
        with (
            mock.patch.object(diagnostics.os, "scandir", side_effect=PermissionError("denied")),
            self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "listed"),
        ):
            self.store.delete_all()
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")),
            self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "removed"),
        ):
            self.store.delete_routine("team_1", ROUTINE)
        self.assertTrue(sealed.exists())
        with (
            mock.patch.object(Path, "rmdir", side_effect=PermissionError("denied")),
            self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "removed"),
        ):
            self.store.delete("team_1")


class RunDiagnosticsViewTests(unittest.TestCase):
    def service(self, store: object) -> SimpleNamespace:
        return SimpleNamespace(
            assistant_lifecycle=SimpleNamespace(_network=lambda _team_id: SimpleNamespace(id=INCARNATION)),
            routine_diagnostics=store,
        )

    def test_a_supervisor_reads_the_current_incarnations_canonical_view(self) -> None:
        reads: list[tuple[object, ...]] = []

        def read(*arguments: object) -> tuple[diagnostics.Diagnostic, ...]:
            reads.append(arguments)
            return (_diagnostic(),)

        view = diagnostics.run_diagnostics(self.service(SimpleNamespace(read=read)), "team_1", RUN, NOW)
        self.assertEqual(reads, [("team_1", INCARNATION, RUN, NOW)])
        self.assertEqual(view, {"team_id": "team_1", "run_id": RUN, "diagnostics": [_diagnostic().view()]})

    def test_unavailable_or_invalid_state_is_one_retryable_problem(self) -> None:
        broken = SimpleNamespace(read=mock.Mock(side_effect=diagnostics.DiagnosticStoreError("down")))
        invalid = SimpleNamespace(read=lambda *_args: (_diagnostic(), _diagnostic()))
        for store in (broken, invalid):
            with self.subTest(store=store), self.assertRaises(ApiProblemError) as caught:
                diagnostics.run_diagnostics(self.service(store), "team_1", RUN, NOW)
            self.assertEqual(
                (caught.exception.status, caught.exception.code),
                (HTTPStatus.SERVICE_UNAVAILABLE, "routine-state-unavailable"),
            )


if __name__ == "__main__":
    unittest.main()
