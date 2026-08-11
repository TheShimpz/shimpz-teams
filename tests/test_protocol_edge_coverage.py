"""Closed edge coverage for the generated Team HTTP protocol mirrors."""

from __future__ import annotations

import unittest
from unittest import mock

from protocol.http.v1 import payload, progress, supervisor


class _Unencodable(str):
    def encode(self, *_args, **_kwargs):
        raise UnicodeError


def _file(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "a" * 32,
        "name": "report.txt",
        "media_type": "text/plain",
        "size": 7,
        "sha256": "b" * 64,
        "created_at": 1,
    }
    value.update(changes)
    return value


def _claims() -> dict[str, object]:
    return {
        "v": 1,
        "aud": "team-local",
        "sub": "a" * 32,
        "session_sha256": "b" * 64,
        "jti": "c" * 32,
        "iat": 2_200_000_000,
        "exp": 2_200_000_015,
        "method": "POST",
        "path": "/v1/teams/team/chat",
        "body": {"kind": "json", "length": 2, "sha256": "d" * 64},
    }


class PayloadEdgeCoverageTests(unittest.TestCase):
    def test_positive_identifiers_metadata_and_storage_projections(self) -> None:
        self.assertEqual(payload.canonical_team_id("team_1"), "team_1")
        self.assertEqual(payload.canonical_assistant_id("assistant-one"), "assistant-one")
        self.assertIsNone(payload.canonical_assistant_id("Bad"))
        self.assertEqual(payload.canonical_team_name("Marketing"), "Marketing")
        self.assertIsNone(payload.canonical_filename("../secret"))
        usage = {"used_bytes": 7, "limit_bytes": 10, "remaining_bytes": 3}
        self.assertEqual(payload.project_storage_usage(usage), usage)

        metadata = _file(**usage)
        projected_metadata = payload.project_file_metadata(metadata, include_usage=True)
        self.assertEqual(projected_metadata["used_bytes"], 7)

        upload = {"team_id": "team", "file": metadata}
        projected_upload = payload.project_storage_response(
            upload,
            kind="upload",
            expected_team_id="team",
            include_team_id=True,
        )
        self.assertEqual(projected_upload["team_id"], "team")
        self.assertEqual(projected_upload["file"]["id"], "a" * 32)

        listing = {"team_id": "team", "files": [_file()], **usage}
        projected_list = payload.project_storage_response(
            listing,
            kind="list",
            expected_team_id="team",
            include_team_id=False,
        )
        self.assertEqual(len(projected_list["files"]), 1)

        deleted = {"team_id": "team", "id": "a" * 32, "deleted": True, **usage}
        projected_delete = payload.project_storage_response(
            deleted,
            kind="delete",
            expected_team_id="team",
            expected_file_id="a" * 32,
            include_team_id=False,
        )
        self.assertEqual(projected_delete["deleted"], True)

    def test_scalar_validators_and_filename_edges(self) -> None:
        self.assertIsNone(payload.canonical_source_digest(None))
        self.assertIsNone(payload.canonical_assurance_handle(None))
        self.assertIsNone(payload.canonical_team_name("bad\nname"))
        self.assertIsNone(payload.canonical_filename(None))
        self.assertIsNone(payload.canonical_filename(_Unencodable("name")))
        self.assertEqual(payload.canonical_media_type(None), "application/octet-stream")
        self.assertIsNone(payload.canonical_media_type(1))
        self.assertIsNone(payload._integer(True))

    def test_storage_usage_and_metadata_fail_closed(self) -> None:
        invalid_usage = (
            None,
            {"used_bytes": True, "limit_bytes": 1, "remaining_bytes": 0},
            {"used_bytes": 1, "limit_bytes": 2, "remaining_bytes": 0},
        )
        for value in invalid_usage:
            self.assertIsNone(payload.project_storage_usage(value))
        self.assertIsNone(payload.project_file_metadata(None, include_usage=False))
        self.assertIsNone(payload.project_file_metadata(_file(id="bad"), include_usage=False))
        self.assertIsNone(payload.project_file_metadata(_file(), include_usage=True))

    def test_storage_response_rejects_each_invalid_shape(self) -> None:
        cases = (
            (None, "list", None),
            ({"team_id": "team", "file": None}, "upload", None),
            ({"team_id": "team", "files": None}, "list", None),
            ({"team_id": "team", "id": "a" * 32, "deleted": True}, "delete", None),
            ({"team_id": "team"}, "unknown", None),
        )
        for value, kind, file_id in cases:
            self.assertIsNone(
                payload.project_storage_response(
                    value,
                    kind=kind,
                    expected_team_id="team",
                    expected_file_id=file_id,
                    include_team_id=False,
                )
            )


class ProgressEdgeCoverageTests(unittest.TestCase):
    def test_helpers_and_events_reject_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            progress._reject_json_constant("NaN")
        with self.assertRaises(ValueError):
            progress._unique_json_object([("x", 1), ("x", 2)])
        cases = (
            None,
            {"phase": "bad", "state": "started", "seq": 1},
            {"phase": "model", "state": "started", "seq": 1, "extra": True},
            {"phase": "model", "state": "started", "seq": True},
            {
                "phase": "action",
                "state": "started",
                "seq": 1,
                "assistant_id": "Bad",
                "index": 1,
                "action": "action",
                "total": 1,
            },
        )
        for value in cases:
            with self.assertRaises(progress.ProgressContractError):
                progress.canonical_event(value)

    def test_records_encoding_and_lines_reject_invalid_values(self) -> None:
        for value in (None, {"type": "terminal", "status": 200}, {"type": "unknown"}):
            with self.assertRaises(progress.ProgressContractError):
                progress.canonical_record(value)
        with self.assertRaises(progress.ProgressContractError):
            progress.encode_record({"type": "terminal", "status": 200, "body": {"bad": object()}})
        with (
            mock.patch.object(progress, "MAX_LINE_BYTES", 1),
            self.assertRaises(progress.ProgressContractError),
        ):
            progress.encode_record({"type": "terminal", "status": 200, "body": {}})
        for raw in (None, b"{}", b'{"x":NaN}\n'):
            with self.assertRaises(progress.ProgressContractError):
                progress.decode_line(raw)
        raw = progress.encode_record({"type": "progress", "seq": 1, "phase": "model", "state": "started"})
        with (
            mock.patch.object(progress, "MAX_PROGRESS_LINE_BYTES", len(raw) - 1),
            self.assertRaises(progress.ProgressContractError),
        ):
            progress.decode_line(raw)


class SupervisorEdgeCoverageTests(unittest.TestCase):
    def test_file_body_is_canonicalized(self) -> None:
        self.assertEqual(
            supervisor._body(
                {
                    "kind": "file",
                    "length": 7,
                    "filename": "report.txt",
                    "media_type": "text/plain",
                }
            ),
            {
                "kind": "file",
                "length": 7,
                "filename": "report.txt",
                "media_type": "text/plain",
            },
        )

    def test_bindings_and_bodies_reject_invalid_values(self) -> None:
        operations = (
            lambda: supervisor._integer(True, label="time"),
            lambda: supervisor._digest("bad", label="digest"),
            lambda: supervisor._body(None),
            lambda: supervisor._body({"kind": "none", "length": 1, "sha256": supervisor.EMPTY_SHA256}),
            lambda: supervisor._body({"kind": "none", "length": 0, "sha256": "a" * 64}),
            lambda: supervisor._body({"kind": "json", "length": 1, "sha256": "a" * 64}),
            lambda: supervisor._body(
                {
                    "kind": "file",
                    "length": 1,
                    "filename": _Unencodable("name"),
                    "media_type": "text/plain",
                }
            ),
            lambda: supervisor._body({"kind": "file", "length": 1, "filename": "name", "media_type": "invalid"}),
            lambda: supervisor._body({"kind": "unknown"}),
            lambda: supervisor._model(None),
            lambda: supervisor._model({"provider": "Bad", "key_sha256": "a" * 64}),
            lambda: supervisor._assurance(None),
            lambda: supervisor._assurance({"kind": "bad", "challenge_id": "a" * 32}),
        )
        for operation in operations:
            with self.assertRaises(supervisor.SupervisorAssertionError):
                operation()

    def test_claims_reject_closed_contract_violations(self) -> None:
        with self.assertRaises(supervisor.SupervisorAssertionError):
            supervisor.canonical_claims(None)
        changes = (
            {"extra": True},
            {"v": 2},
            {"sub": "bad"},
            {"jti": "bad"},
            {"exp": 2_200_000_016},
            {"method": "PATCH"},
            {"path": "/bad/"},
        )
        for change in changes:
            value = _claims()
            value.update(change)
            with self.assertRaises(supervisor.SupervisorAssertionError):
                supervisor.canonical_claims(value)

    def test_json_encoding_is_strict_and_bounded(self) -> None:
        with self.assertRaises(supervisor.SupervisorAssertionError):
            supervisor.canonical_json({"bad": object()})
        with (
            mock.patch.object(supervisor, "ASSERTION_MAX_BYTES", 1),
            self.assertRaises(supervisor.SupervisorAssertionError),
        ):
            supervisor.canonical_json({})


if __name__ == "__main__":
    unittest.main()
