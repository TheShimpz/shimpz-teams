from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hosted import authority

ACCOUNT_ID = "a" * 32
DIGEST = "d" * 64


class AccountAuthorityEdgeTests(unittest.TestCase):
    @staticmethod
    def binding(operation: str = "team-list") -> dict[str, object]:
        return {
            "method": "GET",
            "operation": operation,
            "params": {},
            "query": {},
            "body": {"kind": "none", "length": 0, "sha256": authority.EMPTY_SHA256},
        }

    def test_binding_endpoint_and_capability_reject_invalid_shapes(self) -> None:
        with self.assertRaises(authority.AuthorityUnavailableError):
            authority.binding_digest({})
        with (
            mock.patch.object(
                authority,
                "_REQUEST_VALIDATOR",
                SimpleNamespace(iter_errors=mock.Mock(return_value=())),
            ),
            mock.patch.object(authority.json, "dumps", side_effect=TypeError("invalid")),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority.binding_digest(self.binding())
        with (
            mock.patch.object(authority, "MAX_BINDING_BYTES", 1),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority.binding_digest(self.binding())

        with (
            mock.patch.object(authority, "ACCOUNT_URL", "https://account:7079"),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority._endpoint()

        with tempfile.TemporaryDirectory() as directory:
            capability = Path(directory) / "token"
            capability.write_bytes(b"invalid")
            capability.chmod(0o440)
            with (
                mock.patch.object(authority, "CAPABILITY_FILE", capability),
                self.assertRaises(authority.AuthorityUnavailableError),
            ):
                authority._capability()
        with (
            mock.patch.object(authority.os, "open", side_effect=OSError("missing")),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority._capability()
        with (
            mock.patch.object(authority.os, "open", return_value=3),
            mock.patch.object(
                authority.os,
                "fstat",
                return_value=SimpleNamespace(st_mode=authority.stat.S_IFREG | 0o440),
            ),
            mock.patch.object(authority.os, "read", return_value=b"c" * 64),
            mock.patch.object(authority.os, "close", side_effect=OSError("close failed")),
        ):
            self.assertEqual(authority._capability(), "c" * 64)
        self.assertIsNone(authority._close_descriptor(None))

    def test_payload_and_response_body_reject_encoding_and_framing(self) -> None:
        with self.assertRaises(authority.AuthorityUnavailableError):
            authority._payload("token", {}, None)
        with (
            mock.patch.object(
                authority,
                "_REQUEST_VALIDATOR",
                SimpleNamespace(iter_errors=mock.Mock(return_value=())),
            ),
            mock.patch.object(authority.json, "dumps", side_effect=TypeError("invalid")),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority._payload("token", self.binding(), None)
        with (
            mock.patch.object(authority, "MAX_REQUEST_BYTES", 1),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority._payload("token", self.binding(), None)

        malformed = SimpleNamespace(getheader=mock.Mock(return_value=None), read=mock.Mock())
        with self.assertRaises(authority.AuthorityUnavailableError):
            authority._response_body(malformed)

        raw = b"{}"
        short = SimpleNamespace(
            getheader=mock.Mock(side_effect=lambda name: "application/json" if name == "Content-Type" else "3"),
            read=mock.Mock(return_value=raw),
        )
        with self.assertRaises(authority.AuthorityUnavailableError):
            authority._response_body(short)

        for body in (b"not-json", b"[]"):
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            }
            response = SimpleNamespace(
                getheader=mock.Mock(side_effect=headers.get),
                read=mock.Mock(return_value=body),
            )
            with self.subTest(body=body), self.assertRaises(authority.AuthorityUnavailableError):
                authority._response_body(response)

    def test_evaluation_rejects_identity_and_owner_evidence_mismatches(self) -> None:
        binding = self.binding()
        response = {
            "version": 1,
            "account_id": "invalid",
            "supervisor": False,
            "binding_digest": DIGEST,
        }
        with (
            mock.patch.object(
                authority,
                "_RESPONSE_VALIDATOR",
                SimpleNamespace(iter_errors=mock.Mock(return_value=())),
            ),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority._evaluation(response, binding, DIGEST)

        response["account_id"] = ACCOUNT_ID
        response["owner_account_id"] = ACCOUNT_ID
        with (
            mock.patch.object(
                authority,
                "_RESPONSE_VALIDATOR",
                SimpleNamespace(iter_errors=mock.Mock(return_value=())),
            ),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority._evaluation(response, binding, DIGEST)

        create = self.binding("team-create")
        response.pop("owner_account_id")
        with (
            mock.patch.object(
                authority,
                "_RESPONSE_VALIDATOR",
                SimpleNamespace(iter_errors=mock.Mock(return_value=())),
            ),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority._evaluation(response, create, DIGEST)

    def test_late_success_is_rejected_after_transport_cleanup(self) -> None:
        response = SimpleNamespace(status=200)
        connection = SimpleNamespace(
            request=mock.Mock(),
            getresponse=mock.Mock(return_value=response),
            close=mock.Mock(side_effect=OSError("close failed")),
        )
        with (
            mock.patch.object(authority, "session_token", return_value="token"),
            mock.patch.object(authority, "binding_digest", return_value=DIGEST),
            mock.patch.object(authority, "_payload", return_value=json.dumps({}).encode()),
            mock.patch.object(authority, "_endpoint", return_value=("account", 7079)),
            mock.patch.object(authority, "_capability", return_value="c" * 64),
            mock.patch.object(authority, "_response_body", return_value={}),
            mock.patch.object(authority.http.client, "HTTPConnection", return_value=connection),
            mock.patch.object(authority.time, "monotonic", side_effect=(0.0, 6.0)),
            self.assertRaises(authority.AuthorityUnavailableError),
        ):
            authority.evaluate("token", self.binding())
        connection.close.assert_called_once_with()
        self.assertIsNone(authority._close_connection(None))


if __name__ == "__main__":
    unittest.main()
