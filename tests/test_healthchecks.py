"""State-machine contracts for the Local Controller healthcheck."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from local import healthcheck as local_healthcheck


class _Response:
    def __init__(
        self,
        payload: object,
        *,
        status: int = 200,
        content_type: str = "application/json",
        content_length: str | None = None,
    ) -> None:
        self.status = status
        self.body = json.dumps(payload).encode()
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": content_length or str(len(self.body)),
        }

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)

    def read(self, length: int) -> bytes:
        return self.body[:length]


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request_args = None
        self.closed = False

    def request(self, *args, **kwargs) -> None:
        self.request_args = (args, kwargs)

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


class LocalHealthcheckTests(unittest.TestCase):
    @staticmethod
    def _run(response: _Response, *, token: str = "a" * 64) -> tuple[int, _Connection]:
        connection = _Connection(response)
        with (
            mock.patch.object(Path, "read_text", return_value=token),
            mock.patch.object(local_healthcheck.http.client, "HTTPConnection", return_value=connection),
        ):
            result = local_healthcheck.main()
        return result, connection

    def test_valid_authenticated_health_response_is_accepted_and_connection_is_closed(self) -> None:
        result, connection = self._run(_Response({"status": "ok", "trace_id": "a" * 32}))

        self.assertEqual(result, 0)
        self.assertEqual(
            connection.request_args,
            (("GET", "/healthz"), {"headers": {"Authorization": f"Bearer {'a' * 64}"}}),
        )
        self.assertTrue(connection.closed)

    def test_each_response_contract_failure_is_rejected(self) -> None:
        cases = (
            _Response({"status": "ok", "trace_id": "a" * 32}, status=503),
            _Response({"status": "ok", "trace_id": "a" * 32}, content_type="text/plain"),
            _Response({"status": "ok", "trace_id": "a" * 32}, content_length="0"),
            _Response({"status": "wrong", "trace_id": "a" * 32}),
            _Response({"status": "ok", "trace_id": "short"}),
            _Response({"status": "ok", "trace_id": "a" * 32, "extra": True}),
        )
        for response in cases:
            with self.subTest(response=response):
                result, connection = self._run(response)
                self.assertEqual(result, 1)
                self.assertTrue(connection.closed)

    def test_invalid_token_fails_before_opening_a_connection(self) -> None:
        with (
            mock.patch.object(Path, "read_text", return_value="short"),
            mock.patch.object(local_healthcheck.http.client, "HTTPConnection") as connection,
        ):
            self.assertEqual(local_healthcheck.main(), 1)
        connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
