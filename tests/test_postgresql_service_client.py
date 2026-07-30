from __future__ import annotations

import unittest
from unittest import mock

from controller_runtime import postgresql_service_client


class PostgreSQLServiceClientTests(unittest.TestCase):
    def test_upstream_error_body_is_never_reflected(self) -> None:
        response = mock.Mock(status=502)
        response.read.return_value = b'{"error":"sql and password secret"}'
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with (
            mock.patch.object(postgresql_service_client.http.client, "HTTPConnection", return_value=connection),
            self.assertRaises(postgresql_service_client.PostgreSQLServiceError) as raised,
        ):
            postgresql_service_client._call(
                "/v1/teams/provision",
                {"team_id": "alpha"},
                "a" * 64,
            )

        self.assertEqual(
            str(raised.exception),
            "postgresql-service /v1/teams/provision failed with status 502",
        )
        self.assertNotIn(response.read.return_value.decode(), str(raised.exception))
        self.assertNotIn("password secret", str(raised.exception))
        connection.close.assert_called_once_with()

    def test_success_returns_only_a_json_object(self) -> None:
        response = mock.Mock(status=200)
        response.read.return_value = b'{"created":true}'
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch.object(postgresql_service_client.http.client, "HTTPConnection", return_value=connection):
            result = postgresql_service_client._call(
                "/v1/teams/provision",
                {"team_id": "alpha"},
                "a" * 64,
            )

        self.assertEqual(result, {"created": True})


if __name__ == "__main__":
    unittest.main()
