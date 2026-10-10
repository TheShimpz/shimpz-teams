"""Characterize the strict HTTP boundary decisions."""

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

from core.http import strict as strict_http
from local import app as local_app


class StrictHttpTest(unittest.TestCase):
    @staticmethod
    def _handler(handler_type: type, body: bytes, headers: tuple[tuple[str, str], ...]):
        handler = object.__new__(handler_type)
        handler.headers = Message()
        for name, value in headers:
            handler.headers.add_header(name, value)
        handler.rfile = BytesIO(body)
        return handler

    def test_the_local_wrapper_refuses_an_ambiguous_or_invalid_body(self) -> None:
        cases = (
            (b'{"a":1,"a":2}', (("Content-Type", "application/json"),), HTTPStatus.BAD_REQUEST),
            (b'{"a":NaN}', (("Content-Type", "application/json"),), HTTPStatus.BAD_REQUEST),
            (b"[]", (("Content-Type", "application/json"),), HTTPStatus.UNPROCESSABLE_ENTITY),
            (b"{}", (("Transfer-Encoding", "chunked"),), HTTPStatus.BAD_REQUEST),
            (b"{}", (("Content-Type", "text/plain"),), HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
        )
        for body, extra_headers, expected in cases:
            headers = (("Content-Length", str(len(body))), *extra_headers)
            local = self._handler(local_app.Handler, body, headers)
            with self.subTest(body=body), self.assertRaises(local_app.ApiProblem) as local_error:
                local._capture_body("team-create")
            self.assertEqual(local_error.exception.status, expected)

    def test_the_local_wrapper_rejects_an_encoded_route(self) -> None:
        local = self._handler(local_app.Handler, b"", ())
        local.path = "/v1/teams/%74eam_1"
        local.command = "GET"
        with self.assertRaises(local_app.ApiProblem) as local_error:
            local._resolved_route()
        self.assertEqual(local_error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_the_local_wrapper_reads_the_raw_file_contract(self) -> None:
        body = b"Team private data"
        headers = (
            ("Content-Length", str(len(body))),
            ("Content-Type", "text/plain"),
            ("X-Shimpz-Filename", "brief%20%E2%9C%93.txt"),
        )
        local = self._handler(local_app.Handler, body, headers)

        expected = ("brief ✓.txt", body, "text/plain")
        local._capture_body("file-upload")
        self.assertEqual(local._file_body(), expected)

    def test_file_size_is_rejected_from_content_length_before_body_read(self) -> None:
        headers = Message()
        headers.add_header("Content-Length", "11")
        headers.add_header("Content-Type", "text/plain")
        headers.add_header("X-Shimpz-Filename", "brief.txt")

        # The metadata admission sees only the headers, so an oversized body is refused before any byte is read.
        with self.assertRaises(strict_http.HttpContractError) as error:
            strict_http.file_upload_metadata(headers, max_bytes=10)

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
        self.assertEqual(strict_http.read_json_document(headers, BytesIO(b"{}"), max_bytes=10), (b"{}", {}))

    def test_every_body_entrypoint_admits_only_a_decimal_content_length(self) -> None:
        def headers(length: str) -> Message:
            message = Message()
            message.add_header("Content-Length", length)
            message.add_header("Content-Type", "application/json")
            message.add_header("X-Shimpz-Filename", "file.txt")
            return message

        # int() alone would admit a sign, digit separators, other whitespace, and non-ASCII digits.
        for length in ("+2", "-0", "2_0", "", " ", "\v2", "2\n", "٢", "２", "9" * 5000):
            for read in (
                lambda message: strict_http.read_json_document(message, BytesIO(b"{}"), max_bytes=10),
                lambda message: strict_http.file_upload_metadata(message, max_bytes=10),
                strict_http.reject_body,
            ):
                with self.subTest(length=length), self.assertRaises(strict_http.HttpContractError) as caught:
                    read(headers(length))
                self.assertEqual(
                    (caught.exception.status, caught.exception.code), (HTTPStatus.BAD_REQUEST, "content-length")
                )

        # Optional whitespace around the value is not part of it (RFC 9110).
        self.assertEqual(strict_http.read_json_document(headers(" \t2 "), BytesIO(b"{}"), max_bytes=10)[1], {})
        self.assertEqual(strict_http.file_upload_metadata(headers("\t2"), max_bytes=10).length, 2)
        strict_http.reject_body(headers(" 0\t"))

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
        metadata = strict_http.file_upload_metadata(headers, max_bytes=1)
        self.assertEqual(
            (metadata.filename, strict_http.read_file_content(BytesIO(b"x"), metadata), metadata.media_type),
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
        target = strict_http.parse_request_target("/path?key=value", allow_query=True)
        self.assertEqual(target.query, {"key": "value"})

    def test_bearer_match_refuses_non_ascii_headers_instead_of_raising(self) -> None:
        token = "t" * 43
        for value in (f"Bearer {token}", "Bearer \u00e9", "Bearer \u00c3\u00a9", f"Bearer {token[:-1]}\u00e9"):
            headers = Message()
            headers["Authorization"] = value
            with self.subTest(value=value):
                self.assertIs(strict_http.bearer_matches(headers, token), value == f"Bearer {token}")

    def test_route_groups_cover_every_routing_family(self) -> None:
        expected = {
            "health": "fixed",
            "team-create": "team",
            "file-list": "file",
            "inference-status": "inference",
            "chat-stop": "chat",
            "assistant-integration-list": "assistant-integration",
            "assistant-stored-input-list": "assistant-stored-input",
            "local-assistant-list": "local-assistant",
            "local-assistant-icon": "local-assistant",
            "local-assistant-summary": "local-assistant",
            "local-assistant-details": "local-assistant",
            "unknown": None,
        }
        for operation, group in expected.items():
            with self.subTest(operation=operation):
                self.assertEqual(strict_http.ControllerRouteMatch(operation, {}).group, group)

        uninstall = ("v1", "teams", "team_1", "assistants", "cloudflare-assistant")
        resolved = strict_http.resolve_controller_route("DELETE", uninstall)
        self.assertEqual(resolved.operation, "assistant-uninstall")
        self.assertEqual(
            resolved.params,
            {"team_id": "team_1", "assistant_id": "cloudflare-assistant"},
        )


if __name__ == "__main__":
    unittest.main()
