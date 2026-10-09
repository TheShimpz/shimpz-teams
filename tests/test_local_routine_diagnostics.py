"""Encrypted per-execution Routine diagnostics: AAD, incarnation, retention, bounds, and isolation (ADR-0092)."""

import base64
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

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from local.errors import ApiProblemError
from local.routine import diagnostics
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_run as http_routine_run

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
        position={"phase": "replay", "step": 1},
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
        view = http_routine_run.canonical_diagnostics(
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

    def test_a_sealed_body_is_the_exact_aes_gcm_envelope_of_its_payload(self) -> None:
        nonce = bytes(range(12))
        with mock.patch.object(diagnostics.private_state.os, "urandom", return_value=nonce):
            self.store.record("team_1", INCARNATION, _diagnostic(), ())
        [sealed] = self.files()
        aad = diagnostics._aad("team_1", INCARNATION, sealed.name)
        key = diagnostics._PRIVATE.key(self.store.key_path, "key")
        payload = AESGCM(key).decrypt(nonce, base64.b64decode(json.loads(sealed.read_bytes())["ciphertext"]), aad)
        expected = {
            "algorithm": "AES-256-GCM",
            "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, payload, aad)).decode(),
            "incarnation": INCARNATION,
            "nonce": base64.b64encode(nonce).decode(),
        }
        self.assertEqual(sealed.read_bytes(), json.dumps(expected, separators=(",", ":")).encode())

    def test_aad_binds_team_incarnation_routine_run_operation_attempt_and_instant(self) -> None:
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        # An authentic body of another incarnation is left out; it is never shown, and never mistaken for corruption.
        self.assertEqual(self.store.read("team_1", OTHER_INCARNATION, RUN, NOW), ())
        [sealed] = self.files()
        moved = self.store._team_dir("team_2")
        moved.mkdir(mode=0o700)
        (moved / sealed.name).write_bytes(sealed.read_bytes())
        (moved / sealed.name).chmod(0o600)
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "authentication"):
            self.store.read("team_2", INCARNATION, RUN, NOW)
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
                with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "authentication"):
                    self.store.read("team_1", INCARNATION, RUN, NOW + 2)
                target.unlink()
        self.assertEqual(self.store.read("team_1", INCARNATION, RUN, NOW + 2), (_diagnostic(),))

    def test_a_tampered_or_malformed_body_never_reads_as_evidence(self) -> None:
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        [sealed] = self.files()
        envelope = json.loads(sealed.read_bytes())
        ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
        ciphertext[0] ^= 1
        flipped = {**envelope, "ciphertext": base64.b64encode(bytes(ciphertext)).decode()}
        relabeled = {**envelope, "incarnation": OTHER_INCARNATION}
        # A bit flip in the current incarnation's body, or a body relabeled to look foreign, fails closed.
        for tampered in (flipped, relabeled):
            with self.subTest(tampered=sorted(tampered.items())[1]):
                sealed.write_text(json.dumps(tampered))
                with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "authentication"):
                    self.store.read("team_1", INCARNATION, RUN, NOW)
                with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "authentication"):
                    self.store.read("team_1", OTHER_INCARNATION, RUN, NOW)
        for malformed in (
            b"not json",
            json.dumps({"algorithm": "AES-128-GCM"}).encode(),
            json.dumps({**envelope, "incarnation": "A" * 64}).encode(),
            json.dumps({**envelope, "incarnation": None}).encode(),
            json.dumps({key: value for key, value in envelope.items() if key != "incarnation"}).encode(),
        ):
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
                            "incarnation": INCARNATION,
                            "nonce": base64.b64encode(nonce).decode(),
                            "ciphertext": base64.b64encode(
                                AESGCM(key).encrypt(
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
        with mock.patch.object(http_routine_run, "MAX_RUN_DIAGNOSTICS", 1):
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

    def test_the_family_directory_is_private_and_fails_closed_when_it_is_not(self) -> None:
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        self.store.root.chmod(0o755)
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "ownership contract"):
            self.store.record("team_1", INCARNATION, _diagnostic(attempt=2), ())
        self.assertEqual(len(self.files()), 1)

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


BINDING = diagnostics.RunBinding(ROUTINE, RUN, 2, "sha256:" + "f" * 64, 4)
INPUTS = [{"member": "page", "source": "literal", "value": "1"}]


def _step(step: int = 1, status: str = "done", **changes: object) -> diagnostics.StepRecord:
    placed = {"phase": "replay", "step": step}
    value = diagnostics.StepRecord(BINDING, placed, "shimpz-cloudflare", "list-zones", status, 1, 812, NOW, INPUTS)
    return dataclasses.replace(value, **changes)


class StepRecordCase(unittest.TestCase):
    """A store and a service whose run step pages read it."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = diagnostics.DiagnosticStore(self.root / "bodies", self.root / "key" / "aes256.key")
        self.service = SimpleNamespace(
            assistant_lifecycle=SimpleNamespace(_network=lambda _team_id: SimpleNamespace(id=INCARNATION)),
            routine_diagnostics=self.store,
        )

    def page(self, snapshot: str = "latest", offset: int = 0) -> dict[str, object]:
        return diagnostics.run_steps(self.service, "team_1", RUN, snapshot, offset, NOW + 10)


class StepRecordTests(StepRecordCase):
    """What each step of a run did, its terminal record, and pages of one snapshot (ADR-0092, 2026-10-05, scale)."""

    def test_a_run_shows_each_steps_latest_record_and_proves_which_never_started(self) -> None:
        self.store.record_step("team_1", INCARNATION, _step(1, "failed", attempt=1, duration_ms=10), ())
        self.store.record_step("team_1", INCARNATION, _step(1, "done", attempt=2), ())
        self.store.record_step("team_1", INCARNATION, _step(2, "recovered", duration_ms=None, inputs=None), ())
        live = self.page()
        self.assertEqual(http_routine_run.canonical_run_steps(live), live)
        statuses = [(step["status"], step["attempt"]) for step in live["steps"]]
        # A live run proves nothing about the steps it has no record of.
        self.assertEqual(statuses, [("done", 2), ("recovered", 1), ("unavailable", None), ("unavailable", None)])
        self.assertFalse(live["ended"])
        # Its terminal record proves the steps after the one it reached and dispatched never started.
        self.store.record_run("team_1", INCARNATION, diagnostics.RunRecord(BINDING, 2, True, NOW + 1))
        ended = self.page()
        self.assertEqual([step["status"] for step in ended["steps"]], ["done", "recovered", "unavailable", "not_run"])
        self.store.record_run("team_1", INCARNATION, diagnostics.RunRecord(BINDING, 2, False, NOW + 2))
        self.assertEqual([step["status"] for step in self.page()["steps"]][2:], ["not_run", "not_run"])
        self.assertEqual((ended["revision"], ended["total"], ended["plan_digest"]), (2, 4, BINDING.plan_digest))

    def test_a_page_names_its_snapshot_and_a_changed_record_set_refuses_the_next_page(self) -> None:
        self.store.record_step("team_1", INCARNATION, _step(1), ())
        first = self.page()
        self.assertEqual(self.page(first["snapshot"])["snapshot"], first["snapshot"])
        for change in ("new record", "rewritten"):
            with self.subTest(change=change):
                if change == "new record":
                    self.store.record_step("team_1", INCARNATION, _step(2), ())
                elif change == "rewritten":
                    # The same name, size, and instant, sealed again under a fresh nonce.
                    (path,) = [
                        item for item in self.store._team_dir("team_1").iterdir() if item.name.endswith(".2.step")
                    ]
                    stats, raw = path.stat(), path.read_bytes()
                    document = {**BINDING.document(), **_step(2).view()}
                    with self.store._guard:
                        self.store._seal(
                            "team_1", INCARNATION, (path.name, "step", NOW), document, (), lambda *_args: False
                        )
                    os.utime(path, ns=(stats.st_atime_ns, stats.st_mtime_ns))
                    self.assertEqual((len(path.read_bytes()), path.stat().st_mtime_ns), (len(raw), stats.st_mtime_ns))
                    self.assertNotEqual(path.read_bytes(), raw)
                with self.assertRaises(ApiProblemError) as caught:
                    self.page(first["snapshot"])
                self.assertEqual(
                    (caught.exception.status, caught.exception.code), (HTTPStatus.CONFLICT, "routine-run-changed")
                )
                first = self.page()
        # Expiry changes the retained set too.
        with self.assertRaises(ApiProblemError) as caught:
            diagnostics.run_steps(
                self.service, "team_1", RUN, first["snapshot"], 0, NOW + diagnostics.RETENTION_SECONDS
            )
        self.assertEqual(caught.exception.code, "routine-run-changed")

    def test_pages_of_many_steps_stay_within_their_bound(self) -> None:
        binding = dataclasses.replace(BINDING, total=200)
        wide = [{"member": f"m{index:02d}", "source": "literal", "value": "x" * 120} for index in range(40)]
        for position in range(1, 201):
            self.store.record_step("team_1", INCARNATION, _step(position, binding=binding, inputs=wide), ())
        positions, offset, snapshot = [], 0, "latest"
        while offset is not None:
            page = self.page(snapshot, offset)
            self.assertLessEqual(http_routine.encoded_bytes(page["steps"]), http_routine.MAX_PAGE_BYTES)
            self.assertLess(http_routine.encoded_bytes({**page, "trace_id": "f" * 32}), 128 * 1024)
            positions.extend(step["position"]["step"] for step in page["steps"])
            snapshot, offset = page["snapshot"], page["next"]
        self.assertEqual(positions, list(range(1, 201)))

    def test_records_of_disagreeing_revisions_or_none_at_all_never_show(self) -> None:
        with self.assertRaises(ApiProblemError) as caught:
            self.page()
        self.assertEqual(
            (caught.exception.status, caught.exception.code), (HTTPStatus.NOT_FOUND, "routine-run-steps-not-found")
        )
        self.store.record_step("team_1", INCARNATION, _step(1), ())
        with self.assertRaises(ApiProblemError) as caught:
            self.page(offset=4)
        self.assertEqual(caught.exception.code, "routine-run-steps-not-found")
        self.store.record_step("team_1", INCARNATION, _step(2, binding=dataclasses.replace(BINDING, revision=3)), ())
        with self.assertRaises(ApiProblemError) as caught:
            self.page()
        self.assertEqual(caught.exception.code, "routine-state-unavailable")
        # Another incarnation's records are left out, as its diagnostics are.
        self.store.record_step("team_2", OTHER_INCARNATION, _step(1), ())
        with self.assertRaises(ApiProblemError) as caught:
            diagnostics.run_steps(self.service, "team_2", RUN, "latest", 0, NOW + 10)
        self.assertEqual(caught.exception.code, "routine-run-steps-not-found")

    def test_only_a_valid_secret_free_record_is_ever_sealed(self) -> None:
        invalid = (
            _step(5),
            _step(1, "not_run"),
            _step(1, attempt=0),
            _step(1, binding=dataclasses.replace(BINDING, run_id="bad")),
            _step(1, binding=dataclasses.replace(BINDING, plan_digest="x")),
            _step(1, inputs=[{"member": "page", "source": "secret", "value": "1"}]),
        )
        invalid = (
            *invalid,
            _step(1, "not-permitted"),
            _step(1, position={"phase": "replay", "step": True}),
            _step(1, position={"phase": "planning", "step": 1}),
            # A decision call and its decision-chosen inputs are retired (ADR-0101 amendment, 2026-10-07).
            _step(1, position={"phase": "decision", "call": 1}),
            _step(1, inputs=[{"member": "record_id", "source": "decision", "value": '"r1"'}]),
        )
        for item in invalid:
            with self.subTest(item=item), self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "invalid"):
                self.store.record_step("team_1", INCARNATION, item, ())
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "protected value"):
            self.store.record_step("team_1", INCARNATION, _step(1), ("list-zones",))
        # A protected value is found however the body escapes it: as JSON text or as a preview's escapes.
        for value in ('a"b', "a\nb", "a\u200bb"):
            escaped = _step(1, inputs=[{"member": "page", "source": "literal", "value": json.dumps(value)[1:-1]}])
            with self.subTest(value=value), self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "protected"):
                self.store.record_step("team_1", INCARNATION, escaped, (value,))
        with self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "invalid"):
            self.store.record_run("team_1", INCARNATION, diagnostics.RunRecord(BINDING, 5, True, NOW))
        # A terminal record holding a retired decision count or record never opens as one.
        document = {**BINDING.document(), "reached": 1, "dispatched": True}
        self.assertTrue(diagnostics._run_document(document, RUN))
        for retired in ({"calls": 0}, {"decision": None}, {"calls": 0, "decision": None}):
            with self.subTest(retired=retired):
                self.assertFalse(diagnostics._run_document({**document, **retired}, RUN))
        with (
            mock.patch.object(diagnostics, "MAX_STEP_PLAINTEXT_BYTES", 64),
            self.assertRaisesRegex(diagnostics.DiagnosticStoreError, "byte limit"),
        ):
            self.store.record_step("team_1", INCARNATION, _step(1), ())

    def test_deleting_a_routine_removes_its_step_and_run_records(self) -> None:
        self.store.record_step("team_1", INCARNATION, _step(1), ())
        self.store.record_run("team_1", INCARNATION, diagnostics.RunRecord(BINDING, 1, False, NOW))
        self.store.record("team_1", INCARNATION, _diagnostic(), ())
        self.assertEqual(len(list(self.store._team_dir("team_1").iterdir())), 3)
        self.store.delete_routine("team_1", ROUTINE)
        self.assertEqual(list(self.store._team_dir("team_1").iterdir()), [])
        # Failure diagnostics never read a step record.
        self.store.record_step("team_1", INCARNATION, _step(1), ())
        self.assertEqual(self.store.read("team_1", INCARNATION, RUN, NOW + 10), ())

    def test_a_record_that_does_not_open_fails_the_page_closed(self) -> None:
        self.store.record_step("team_1", INCARNATION, _step(1), ())
        (path,) = list(self.store._team_dir("team_1").iterdir())
        envelope = json.loads(path.read_bytes())
        path.write_bytes(json.dumps({**envelope, "nonce": envelope["nonce"][::-1]}).encode())
        with self.assertRaises(ApiProblemError) as caught:
            self.page()
        self.assertEqual(caught.exception.code, "routine-state-unavailable")


class StepRecordOrderTests(StepRecordCase):
    def test_the_latest_record_by_sequence_wins_even_after_the_clock_stepped_back(self) -> None:
        self.store.record_step("team_1", INCARNATION, _step(1, "failed", recorded_at=NOW + 5), ())
        self.store.record_step("team_1", INCARNATION, _step(1, "done", recorded_at=NOW), ())
        self.assertEqual(self.page()["steps"][0]["status"], "done")

    def test_a_record_that_is_not_an_object_or_a_page_that_is_not_canonical_fails_closed(self) -> None:
        name = f"{NOW}.{ROUTINE}.{RUN}.1.step"
        with self.store._guard:
            self.store._seal("team_1", INCARNATION, (name, "step", NOW), ["not", "an", "object"], (), lambda *_a: False)
        with self.assertRaises(ApiProblemError) as caught:
            self.page()
        self.assertEqual(caught.exception.code, "routine-state-unavailable")
        self.store.delete("team_1")
        self.store.record_step("team_1", INCARNATION, _step(1), ())
        with (
            mock.patch.object(diagnostics.http_routine_run, "canonical_run_steps", return_value=None),
            self.assertRaises(ApiProblemError) as caught,
        ):
            self.page()
        self.assertEqual(caught.exception.code, "routine-state-unavailable")


class RunPageEdgeTests(StepRecordCase):
    def test_an_old_run_pages_from_its_own_records_with_no_routine_at_all(self) -> None:
        # The service reads no Routine state: a run of a replaced or deleted revision still renders.
        self.assertFalse(hasattr(self.service, "routine_store"))
        self.store.record_step("team_1", INCARNATION, _step(1, binding=dataclasses.replace(BINDING, revision=1)), ())
        self.assertEqual(self.page()["revision"], 1)

    def test_a_sealed_record_in_a_retired_decision_shape_fails_the_page_closed(self) -> None:
        """An authentic body holding a decision call or decision record never reads as a run's record (2026-10-07)."""
        step = {**BINDING.document(), **_step(1).view(), "position": {"phase": "decision", "call": 1}}
        run = {**BINDING.document(), "reached": 1, "dispatched": False, "calls": 0, "decision": None}
        for kind, document in (("step", step), ("run", run)):
            self.store.delete("team_1")
            self.store.record_step("team_1", INCARNATION, _step(1), ())
            name = f"{NOW}.{ROUTINE}.{RUN}.{'9.step' if kind == 'step' else 'run'}"
            with self.subTest(kind=kind):
                with self.store._guard:
                    self.store._seal("team_1", INCARNATION, (name, kind, NOW), document, (), lambda *_a: False)
                with self.assertRaises(ApiProblemError) as caught:
                    self.page()
                self.assertEqual(caught.exception.code, "routine-state-unavailable")

    def test_a_record_keys_by_its_replay_step_and_anything_else_by_none(self) -> None:
        self.assertEqual(diagnostics.position_key({"phase": "replay", "step": 3}), 3)
        self.assertEqual(diagnostics.position_key(None), 0)
        self.assertEqual(diagnostics.position_key({"phase": "decision", "call": 4}), 0)


class FailureEvidenceTests(unittest.TestCase):
    """A failed attempt's diagnostic is redacted against the run's whole protection, and withheld after its loss."""

    def failed(self, failure: dict[str, object]) -> Exception:
        from action import failure as action_failure

        problem = ApiProblemError(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-action-failed")
        problem.__cause__ = action_failure.ActionFailedError(action_failure.ActionFailure(**failure))
        return problem

    def test_a_value_an_earlier_step_returned_in_secret_never_reaches_the_diagnostic(self) -> None:
        from routine import trace

        echoed = {**FAILURE, "message": "Bad key SchemaSecretAbc rejected", "response_excerpt": "SCHEMASECRETABC"}
        protection = trace.Protection().grow(("SchemaSecretAbc",))
        failure, condition = diagnostics.evidence(self.failed(echoed), protection)
        self.assertIsNone(condition)
        self.assertNotIn("schemasecretabc", json.dumps(failure).lower())
        self.assertTrue(failure["redacted"])
        self.assertEqual(http_routine_run.canonical_failure(failure), failure)

    def test_after_the_run_lost_its_protection_only_the_status_is_kept(self) -> None:
        from routine import trace

        failure, _condition = diagnostics.evidence(self.failed(FAILURE), trace.Protection(lost=True))
        self.assertEqual(
            failure,
            {
                "error_type": "withheld",
                "message": "",
                "provider": None,
                "http_status": 404,
                "response_excerpt": None,
                "redacted": True,
                "truncated": False,
            },
        )
        self.assertEqual(http_routine_run.canonical_failure(failure), failure)
        self.assertIsNone(diagnostics.evidence(ValueError("other"), trace.Protection()))


if __name__ == "__main__":
    unittest.main()
