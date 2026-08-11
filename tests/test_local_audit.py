"""Durability and metadata contracts for the local audit journal."""

from __future__ import annotations

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
        synchronized_at: list[float] = []

        def observe(descriptor: int) -> None:
            real_fsync(descriptor)
            synchronized_at.append(time.monotonic())
            synchronized.set()

        window = 0.02
        with (
            mock.patch.object(audit, "AUDIT_PATH", self.path),
            mock.patch.object(audit, "GROUP_COMMIT_MAX_SECONDS", window),
            mock.patch.object(audit.os, "fsync", side_effect=observe) as sync,
        ):
            started = time.monotonic()
            _record("first", result="ok")
            _record("second", result="ok")
            self.assertTrue(synchronized.wait(timeout=1))
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, window * 5)
        self.assertLess(synchronized_at[0] - started, window * 5)
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
        with (
            mock.patch.object(audit, "_stopping", True),
            mock.patch.object(audit, "_failure", None),
            mock.patch.object(audit, "_descriptor", None),
            mock.patch.object(audit, "_dirty_since", None),
        ):
            audit._flush_worker()

        with (
            mock.patch.object(audit, "_stopping", False),
            mock.patch.object(audit, "_failure", RuntimeError("failed")),
            mock.patch.object(audit, "_descriptor", None),
        ):
            audit._flush_worker()

        def stop_after_wait(*_args: object, **_kwargs: object) -> None:
            audit._stopping = True

        with (
            mock.patch.object(audit, "_stopping", False),
            mock.patch.object(audit, "_failure", None),
            mock.patch.object(audit, "_dirty_since", None),
            mock.patch.object(audit._CONDITION, "wait", side_effect=stop_after_wait),
        ):
            audit._flush_worker()

        with (
            mock.patch.object(audit, "_stopping", False),
            mock.patch.object(audit, "_failure", None),
            mock.patch.object(audit, "_dirty_since", time.monotonic()),
            mock.patch.object(audit._CONDITION, "wait", side_effect=stop_after_wait),
        ):
            audit._flush_worker()

        with (
            mock.patch.object(audit, "_stopping", False),
            mock.patch.object(audit, "_failure", None),
            mock.patch.object(audit, "_dirty_since", 0.0),
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
