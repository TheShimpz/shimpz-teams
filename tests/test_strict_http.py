"""Characterize the shared hosted/local HTTP boundary decisions."""

from __future__ import annotations

import sys
import unittest
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hosted_assistant_fixture import app, hosted_controller, runtime_state

from core.http import strict as strict_http
from local import app as local_app


class SharedStrictHttpTest(unittest.TestCase):
    @staticmethod
    def _handler(handler_type: type, body: bytes, headers: tuple[tuple[str, str], ...]):
        handler = object.__new__(handler_type)
        handler.headers = Message()
        for name, value in headers:
            handler.headers.add_header(name, value)
        handler.rfile = BytesIO(body)
        return handler

    def test_hosted_and_local_wrappers_make_the_same_body_decision(self) -> None:
        cases = (
            (b'{"a":1,"a":2}', (("Content-Type", "application/json"),), HTTPStatus.BAD_REQUEST),
            (b'{"a":NaN}', (("Content-Type", "application/json"),), HTTPStatus.BAD_REQUEST),
            (b"[]", (("Content-Type", "application/json"),), HTTPStatus.UNPROCESSABLE_ENTITY),
            (b"{}", (("Transfer-Encoding", "chunked"),), HTTPStatus.BAD_REQUEST),
        )
        for body, extra_headers, expected in cases:
            headers = (("Content-Length", str(len(body))), *extra_headers)
            hosted = self._handler(app.Handler, body, headers)
            local = self._handler(local_app.Handler, body, headers)
            with self.subTest(body=body):
                with self.assertRaises(runtime_state.ApiError) as hosted_error:
                    hosted._capture_body("team-create")
                with self.assertRaises(local_app.ApiProblem) as local_error:
                    local._capture_body("team-create")
                self.assertEqual((hosted_error.exception.status, local_error.exception.status), (expected, expected))

    def test_hosted_and_local_wrappers_reject_the_same_encoded_route(self) -> None:
        hosted = self._handler(app.Handler, b"", ())
        hosted.path = "/v1/teams/%74eam_1"
        local = self._handler(local_app.Handler, b"", ())
        local.path = hosted.path

        with self.assertRaises(runtime_state.ApiError) as hosted_error:
            hosted_controller.hosted.route_target(hosted.headers, hosted.path, "GET", runtime_state.ApiError)
        with self.assertRaises(local_app.ApiProblem) as local_error:
            local.command = "GET"
            local._resolved_route()

        self.assertEqual(
            (hosted_error.exception.status, local_error.exception.status),
            (HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST),
        )

    def test_hosted_and_local_wrappers_read_the_same_raw_file_contract(self) -> None:
        body = b"Team private data"
        headers = (
            ("Content-Length", str(len(body))),
            ("Content-Type", "text/plain"),
            ("X-Shimpz-Filename", "brief%20%E2%9C%93.txt"),
        )
        hosted = self._handler(app.Handler, body, headers)
        local = self._handler(local_app.Handler, body, headers)

        expected = ("brief ✓.txt", body, "text/plain")
        hosted._capture_body("file-upload")
        self.assertEqual(hosted._read_file_body(), expected)
        local._capture_body("file-upload")
        self.assertEqual(local._file_body(), expected)

    def test_file_size_is_rejected_from_content_length_before_body_read(self) -> None:
        class Unreadable:
            @staticmethod
            def read(_length: int) -> bytes:
                raise AssertionError("oversized body must not be read")

        headers = Message()
        headers.add_header("Content-Length", "11")
        headers.add_header("Content-Type", "text/plain")
        headers.add_header("X-Shimpz-Filename", "brief.txt")

        with self.assertRaises(strict_http.HttpContractError) as error:
            strict_http.read_file_upload(headers, Unreadable(), max_bytes=10)

        self.assertEqual(error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_file_metadata_rejects_every_path_and_control_name_before_body_read(self) -> None:
        for encoded_name in (".", "..", "..%2Fsecret", "path%5Csecret", "%20name", "name%20", "%00name"):
            headers = Message()
            headers.add_header("Content-Length", "1")
            headers.add_header("Content-Type", "text/plain")
            headers.add_header("X-Shimpz-Filename", encoded_name)
            with (
                self.subTest(encoded_name=encoded_name),
                self.assertRaises(strict_http.HttpContractError) as caught,
            ):
                strict_http.file_upload_metadata(headers, max_bytes=1)
            self.assertEqual(caught.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_json_document_rejects_missing_invalid_short_and_non_object_bodies(self) -> None:
        cases = (
            ((), b"{}"),
            (("invalid",), b"{}"),
            (("3",), b"{}"),
            (("2",), b"[]"),
        )
        for lengths, body in cases:
            headers = Message()
            for length in lengths:
                headers.add_header("Content-Length", length)
            headers.add_header("Content-Type", "application/json")
            with self.subTest(lengths=lengths, body=body), self.assertRaises(strict_http.HttpContractError):
                strict_http.read_json_document(headers, BytesIO(body), max_bytes=10)

        headers = Message()
        headers.add_header("Content-Length", "2")
        headers.add_header("Content-Type", "application/json")
        self.assertEqual(strict_http.read_json_object(headers, BytesIO(b"{}"), max_bytes=10), {})

    def test_file_metadata_and_content_reject_framing_encoding_and_io_errors(self) -> None:
        invalid_headers = (
            (("Transfer-Encoding", "chunked"),),
            (),
            (("Content-Length", "invalid"),),
            (("Content-Length", "1"), ("Content-Type", "INVALID")),
            (
                ("Content-Length", "1"),
                ("Content-Type", "text/plain"),
                ("X-Shimpz-Filename", "%FF"),
            ),
        )
        for values in invalid_headers:
            headers = Message()
            for name, value in values:
                headers.add_header(name, value)
            with self.subTest(values=values), self.assertRaises(strict_http.HttpContractError):
                strict_http.file_upload_metadata(headers, max_bytes=10)

        metadata = strict_http.FileUploadMetadata(2, "file.txt", "text/plain")
        for stream in (
            BytesIO(b"x"),
            SimpleNamespace(read=lambda _length: (_ for _ in ()).throw(OSError("offline"))),
        ):
            with self.subTest(stream=stream), self.assertRaises(strict_http.HttpContractError):
                strict_http.read_file_content(stream, metadata)

        headers = Message()
        headers.add_header("Content-Length", "1")
        headers.add_header("Content-Type", "text/plain")
        headers.add_header("X-Shimpz-Filename", "file.txt")
        self.assertEqual(
            strict_http.read_file_upload(headers, BytesIO(b"x"), max_bytes=1),
            ("file.txt", b"x", "text/plain"),
        )

    def test_bodyless_and_target_parsing_reject_ambiguous_framing_and_routes(self) -> None:
        for values in (
            (("Transfer-Encoding", "chunked"),),
            (("Content-Length", "0"), ("Content-Length", "0")),
            (("Content-Length", "invalid"),),
            (("Content-Length", "1"),),
        ):
            headers = Message()
            for name, value in values:
                headers.add_header(name, value)
            with self.subTest(values=values), self.assertRaises(strict_http.HttpContractError):
                strict_http.reject_body(headers)
        strict_http.reject_body(Message())
        empty = Message()
        empty.add_header("Content-Length", "0")
        strict_http.reject_body(empty)

        for target, allow_query, maximum in (
            ("/path", False, 1),
            ("/path?query=1", False, 100),
            ("/path?=value", True, 100),
            ("/path?key=one&key=two", True, 100),
        ):
            with self.subTest(target=target), self.assertRaises(strict_http.HttpContractError):
                strict_http.parse_request_target(target, allow_query=allow_query, max_bytes=maximum)
        target = strict_http.parse_routed_request(
            Message(),
            "/path?key=value",
            "POST",
            body_methods=frozenset({"POST"}),
            allow_query=True,
        )
        self.assertEqual(target.query, {"key": "value"})

    def test_route_groups_and_profile_validation_cover_every_routing_family(self) -> None:
        expected = {
            "health": "fixed",
            "team-create": "team",
            "file-list": "file",
            "inference-status": "inference",
            "chat-stream": "chat",
            "assistant-integration-list": "assistant-integration",
            "unknown": None,
        }
        for operation, group in expected.items():
            with self.subTest(operation=operation):
                self.assertEqual(strict_http.ControllerRouteMatch(operation, {}).group, group)
        with self.assertRaises(ValueError):
            strict_http.resolve_controller_route("unknown", "GET", ())


if __name__ == "__main__":
    unittest.main()
