"""Durability and metadata contracts for the local audit journal."""

import hashlib
import json
import multiprocessing
import os
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from local import audit


def _record(operation: str, *, result: str, **metadata: object) -> str:
    return audit.record(
        operation,
        result=result,
        principal=audit.AuditPrincipal("team-local", "machine"),
        **metadata,
    )


def _crash_after_acknowledged_audit(path: str, sync_marker: str) -> None:
    audit.AUDIT_PATH = Path(path)
    audit.GROUP_COMMIT_MAX_SECONDS = 60
    real_fsync = os.fsync

    def mark_sync(descriptor: int) -> None:
        real_fsync(descriptor)
        Path(sync_marker).write_text("synced", encoding="ascii")

    audit.os.fsync = mark_sync
    _record("assistant-action", result="ok", team_id="team_1")
    os._exit(0)


def _chained(*files: Path) -> list[dict[str, object]]:
    """Every event of the files, oldest first, after proving each line carries the SHA-256 of the line before it."""
    lines = [line for path in files for line in path.read_bytes().splitlines(keepends=True)]
    previous = audit.GENESIS
    for line in lines:
        if json.loads(line)["prev_sha256"] != previous:
            raise AssertionError("the audit chain is broken")
        previous = hashlib.sha256(line).hexdigest()
    return [json.loads(line) for line in lines]


class LocalAuditChainTests(unittest.TestCase):
    def setUp(self) -> None:
        audit.close()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(audit.close)
        self.path = Path(self.temporary.name) / "audit" / "audit.jsonl"
        patcher = mock.patch.object(audit, "AUDIT_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_line_carries_the_previous_lines_hash_across_restarts(self) -> None:
        _record("first", result="ok")
        _record("second", result="ok")
        audit.close()
        _record("after-restart", result="ok")
        audit.close()
        events = _chained(self.path)
        self.assertEqual([event["operation"] for event in events], ["first", "second", "after-restart"])
        self.assertEqual(events[0]["prev_sha256"], audit.GENESIS)

    def test_a_rotated_in_file_starts_from_the_last_line_of_the_file_it_replaced(self) -> None:
        rotated = self.path.with_name(f"{self.path.name}.1")
        with mock.patch.object(audit, "MAX_BYTES", 1):
            _record("first", result="ok")
            _record("second", result="ok")
        audit.close()
        self.assertEqual([event["operation"] for event in _chained(rotated, self.path)], ["first", "second"])
        # A restart after a rotation that wrote nothing yet continues from the newest rotated file.
        self.path.write_bytes(b"")
        self.path.chmod(0o600)
        _record("third", result="ok")
        audit.close()
        self.assertEqual([event["operation"] for event in _chained(rotated, self.path)], ["first", "third"])

    def test_a_torn_final_write_is_ended_and_keeps_its_place_in_the_chain(self) -> None:
        _record("first", result="ok")
        audit.close()
        with self.path.open("ab") as journal:
            journal.write(b'{"torn":')
        _record("after-crash", result="ok")
        audit.close()
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.assertEqual(lines[1], b'{"torn":\n')
        self.assertEqual(json.loads(lines[2])["prev_sha256"], hashlib.sha256(lines[1]).hexdigest())
        self.assertEqual(json.loads(lines[2])["operation"], "after-crash")

    def test_an_altered_removed_or_reordered_line_breaks_the_chain(self) -> None:
        for operation in ("first", "second", "third"):
            _record(operation, result="ok")
        audit.close()
        lines = self.path.read_bytes().splitlines(keepends=True)
        altered = lines[1].replace(b'"second"', b'"forged"')
        for tampered in ([lines[0], altered, lines[2]], [lines[0], lines[2]], [lines[1], lines[0], lines[2]]):
            with self.subTest(tampered=tampered):
                self.path.write_bytes(b"".join(tampered))
                with self.assertRaisesRegex(AssertionError, "chain is broken"):
                    _chained(self.path)


class LocalAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        audit.close()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(audit.close)
        self.path = Path(self.temporary.name) / "audit" / "audit.jsonl"

    def test_acknowledged_event_survives_process_crash_before_group_sync(self) -> None:
        marker = Path(self.temporary.name) / "sync-marker"
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_after_acknowledged_audit,
            args=(str(self.path), str(marker)),
        )

        process.start()
        process.join(timeout=10)

        self.assertEqual(process.exitcode, 0)
        self.assertFalse(marker.exists())
        event = json.loads(self.path.read_bytes())
        self.assertEqual(event["operation"], "assistant-action")
        self.assertEqual(event["team_id"], "team_1")

    def test_multiple_events_share_one_durability_sync(self) -> None:
        with (
            mock.patch.object(audit, "AUDIT_PATH", self.path),
            mock.patch.object(audit, "GROUP_COMMIT_MAX_SECONDS", 60),
            mock.patch.object(audit.os, "fsync", wraps=os.fsync) as sync,
        ):
            _record("first", result="ok")
            _record("second", result="ok")
            self.assertEqual(sync.call_count, 0)
            audit.flush()

        self.assertEqual(sync.call_count, 1)

    def test_background_sync_bounds_the_acknowledged_loss_window(self) -> None:
        synchronized = threading.Event()
        real_fsync = os.fsync
        real_wait = audit._CONDITION.wait
        scheduled_waits: list[float] = []

        def observe(descriptor: int) -> None:
            real_fsync(descriptor)
            synchronized.set()

        def observe_wait(timeout: float | None = None) -> bool:
            if timeout is not None:
                scheduled_waits.append(timeout)
            return real_wait(timeout)

        window = 0.02
        with (
            mock.patch.object(audit, "AUDIT_PATH", self.path),
            mock.patch.object(audit, "GROUP_COMMIT_MAX_SECONDS", window),
            mock.patch.object(audit.os, "fsync", side_effect=observe) as sync,
            mock.patch.object(audit._CONDITION, "wait", side_effect=observe_wait),
        ):
            _record("first", result="ok")
            _record("second", result="ok")
            self.assertTrue(synchronized.wait(timeout=1))

        self.assertTrue(any(0 < timeout <= window for timeout in scheduled_waits))
        self.assertEqual(sync.call_count, 1)

    def test_action_loss_model_limits_loss_to_the_current_unsynced_group(self) -> None:
        durable_snapshot = b""
        real_fsync = os.fsync

        def checkpoint(descriptor: int) -> None:
            nonlocal durable_snapshot
            real_fsync(descriptor)
            durable_snapshot = self.path.read_bytes()

        with (
            mock.patch.object(audit, "AUDIT_PATH", self.path),
            mock.patch.object(audit, "GROUP_COMMIT_MAX_SECONDS", 60),
            mock.patch.object(audit.os, "fsync", side_effect=checkpoint),
        ):
            for index in range(8):
                _record("burst", result="ok", detail=str(index))
            self.assertEqual(durable_snapshot, b"")
            audit.flush()
            self.assertEqual(len(durable_snapshot.splitlines()), 8)
            _record("next-group", result="ok")
            simulated_recovery = durable_snapshot

        self.assertEqual(len(simulated_recovery.splitlines()), 8)

    def test_concurrent_records_are_complete_and_share_the_writer(self) -> None:
        with (
            mock.patch.object(audit, "AUDIT_PATH", self.path),
            mock.patch.object(audit, "GROUP_COMMIT_MAX_SECONDS", 60),
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            trace_ids = tuple(
                executor.map(
                    lambda index: _record("concurrent", result="ok", detail=str(index)),
                    range(64),
                )
            )
            audit.flush()

        events = tuple(json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(len(events), 64)
        self.assertEqual({event["trace_id"] for event in events}, set(trace_ids))

    def test_request_context_file_metadata_and_rotation_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "principal is unavailable"):
            audit.record_request("operation", result="ok")

        unsafe = Path(self.temporary.name) / "unsafe.jsonl"
        unsafe.write_text("event", encoding="ascii")
        unsafe.chmod(stat.S_IMODE(unsafe.stat().st_mode) | stat.S_IRGRP)
        with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
            audit._safe_file(unsafe)

        rotating = Path(self.temporary.name) / "rotate.jsonl"
        rotating.write_bytes(b"new")
        rotating.chmod(0o600)
        first = rotating.with_name(f"{rotating.name}.1")
        second = rotating.with_name(f"{rotating.name}.2")
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        first.chmod(0o600)
        second.chmod(0o600)
        with mock.patch.object(audit, "MAX_BYTES", 1):
            audit._rotate(rotating)
        self.assertEqual(first.read_bytes(), b"new")
        self.assertEqual(second.read_bytes(), b"first")

    def test_failure_sync_and_write_states_are_explicit(self) -> None:
        failure = RuntimeError("failed")
        with mock.patch.object(audit, "_failure", failure), self.assertRaisesRegex(RuntimeError, "failed"):
            audit._raise_failure_locked()
        with mock.patch.object(audit, "_stopping", True), self.assertRaisesRegex(RuntimeError, "closing"):
            audit._raise_failure_locked()

        with (
            mock.patch.object(audit, "_descriptor", 123),
            mock.patch.object(audit, "_dirty_since", 0.0),
            mock.patch.object(audit, "_failure", None),
            mock.patch.object(audit.os, "fsync", side_effect=OSError("failed")),
            self.assertRaisesRegex(RuntimeError, "could not be synchronized"),
        ):
            audit._sync_locked()

        with mock.patch.object(audit.os, "write", return_value=0), self.assertRaisesRegex(RuntimeError, "incomplete"):
            audit._write_all(1, b"event")

    def test_open_writer_detects_replacement_and_rotates_oversized_descriptor(self) -> None:
        with mock.patch.object(audit, "AUDIT_PATH", self.path):
            descriptor = audit._open_descriptor_locked()
            self.path.unlink()
            self.path.write_bytes(b"replacement")
            self.path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "changed while open"):
                audit._open_descriptor_locked()
            audit._close_descriptor_locked()

            descriptor = audit._open_descriptor_locked()
            os.write(descriptor, b"oversized")
            audit._dirty_since = time.monotonic()
            with mock.patch.object(audit, "MAX_BYTES", 1), audit._CONDITION:
                audit._open_descriptor_locked()
            self.assertTrue(self.path.with_name(f"{self.path.name}.1").exists())
            audit._close_descriptor_locked()
            audit._dirty_since = None

    def test_flush_worker_exits_for_stop_failure_wait_and_sync_failure(self) -> None:
        with mock.patch.multiple(audit, _stopping=True, _failure=None, _descriptor=None, _dirty_since=None):
            audit._flush_worker()

        with mock.patch.multiple(audit, _stopping=False, _failure=RuntimeError("failed"), _descriptor=None):
            audit._flush_worker()

        def stop_after_wait(*_args: object, **_kwargs: object) -> None:
            audit._stopping = True

        with (
            mock.patch.multiple(audit, _stopping=False, _failure=None, _dirty_since=None),
            mock.patch.object(audit._CONDITION, "wait", side_effect=stop_after_wait),
        ):
            audit._flush_worker()

        with (
            mock.patch.multiple(audit, _stopping=False, _failure=None, _dirty_since=time.monotonic()),
            mock.patch.object(audit._CONDITION, "wait", side_effect=stop_after_wait),
        ):
            audit._flush_worker()

        with (
            mock.patch.multiple(audit, _stopping=False, _failure=None, _dirty_since=0.0),
            mock.patch.object(audit, "_sync_locked", side_effect=RuntimeError("failed")),
        ):
            audit._flush_worker()

    def test_record_validates_metadata_and_maps_writer_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "principal metadata"):
            audit.record(
                "operation",
                result="ok",
                principal=audit.AuditPrincipal("invalid", "human"),
            )
        with self.assertRaisesRegex(ValueError, "trace id"):
            audit.record(
                "operation",
                result="ok",
                principal=audit.AuditPrincipal("team-local", "machine", trace_id="invalid"),
            )

        principal = audit.AuditPrincipal(
            "team-local",
            "machine",
            credential_state="machine_bearer_present",
            trace_id="a" * 32,
        )
        with mock.patch.object(audit, "AUDIT_PATH", self.path):
            with audit.bind_request_principal(principal):
                trace_id = audit.record_request(
                    "operation",
                    result="ok",
                    team_id="team_1",
                    assistant="helper",
                )
            audit.flush()
        event = json.loads(self.path.read_bytes())
        self.assertEqual(trace_id, "a" * 32)
        self.assertEqual(event["credential_state"], "machine_bearer_present")
        self.assertEqual(event["team_id"], "team_1")
        self.assertEqual(event["assistant"], "helper")

        with (
            mock.patch.object(audit, "_open_descriptor_locked", side_effect=OSError("failed")),
            self.assertRaisesRegex(RuntimeError, "could not be written"),
        ):
            audit.record("operation", result="ok", principal=principal)


if __name__ == "__main__":
    unittest.main()
