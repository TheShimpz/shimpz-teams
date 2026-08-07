from __future__ import annotations

import http.client
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hosted.install import developers_client
from install.contract import ContractValidationError


class DevelopersClientEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("t" * 48, encoding="ascii")
        self.client = developers_client.DevelopersClient(self.token)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_authorization_icon_and_json_failures_are_closed(self) -> None:
        with (
            mock.patch.object(
                developers_client._CONTRACTS,
                "validate",
                side_effect=ContractValidationError("invalid"),
            ),
            self.assertRaises(developers_client.DevelopersClientError),
        ):
            self.client.authorize_install({})

        with (
            mock.patch.object(developers_client._CONTRACTS, "validate"),
            mock.patch.object(self.client, "_request", return_value=(500, {})),
            self.assertRaises(developers_client.DevelopersClientError),
        ):
            self.client.authorize_install({"request": "valid"})

        self.client._raw_request = mock.Mock(return_value=(404, b""))
        with self.assertRaises(developers_client.AssistantNotInstallableError):
            self.client.icon("sha256:" + "a" * 64, "sha256:" + "b" * 64)
        self.client._raw_request.return_value = (200, b"wrong")
        with self.assertRaises(developers_client.DevelopersClientError):
            self.client.icon("sha256:" + "a" * 64, "sha256:" + "b" * 64)

        self.client._raw_request.return_value = (204, b"")
        self.assertEqual(self.client._request("GET", "/", None), (204, None))
        self.client._raw_request.return_value = (200, b"not-json")
        with self.assertRaises(developers_client.DevelopersClientError):
            self.client._request("GET", "/", None)

    def test_transport_errors_and_response_limit_close_connection(self) -> None:
        connection = SimpleNamespace(
            request=mock.Mock(side_effect=http.client.HTTPException("failed")),
            close=mock.Mock(),
        )
        with (
            mock.patch.object(http.client, "HTTPConnection", return_value=connection),
            self.assertRaises(developers_client.DevelopersClientError),
        ):
            self.client._raw_request("GET", "/", None, accept="application/json")
        connection.close.assert_called_once_with()

        response = SimpleNamespace(
            status=200,
            read=mock.Mock(return_value=b"x" * (developers_client._MAX_RESPONSE_BYTES + 1)),
        )
        connection = SimpleNamespace(
            request=mock.Mock(),
            getresponse=mock.Mock(return_value=response),
            close=mock.Mock(),
        )
        with (
            mock.patch.object(http.client, "HTTPConnection", return_value=connection),
            self.assertRaises(developers_client.DevelopersClientError),
        ):
            self.client._raw_request("POST", "/", {}, accept="application/json")
        connection.close.assert_called_once_with()

    def test_token_and_schema_helpers_reject_invalid_inputs(self) -> None:
        missing = Path(self.directory.name) / "missing"
        with self.assertRaises(RuntimeError):
            developers_client._read_service_token(missing)
        self.token.write_text("short", encoding="ascii")
        with self.assertRaises(RuntimeError):
            developers_client._read_service_token(self.token)

        with (
            mock.patch.object(
                developers_client._CONTRACTS,
                "validate",
                side_effect=ContractValidationError("invalid"),
            ),
            self.assertRaises(developers_client.DevelopersClientError),
        ):
            developers_client._validated("schema.json", {})
        with (
            mock.patch.object(developers_client._CONTRACTS, "validate"),
            self.assertRaises(developers_client.DevelopersClientError),
        ):
            developers_client._validated("schema.json", [])


if __name__ == "__main__":
    unittest.main()
