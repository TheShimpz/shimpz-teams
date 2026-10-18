"""The Browser's watched screen over HTTP: the open route and the stream route's exact upgrade and refusals."""

import unittest
from contextlib import contextmanager
from email.message import Message
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from local.browser.runtime import BrowserError
from local.errors import ApiProblemError
from local.http import browser as local_http_browser
from local.http import server
from protocol.http.v1 import browser_view as view_protocol
from tests.test_local_http_server_edges import LocalHttpEdgeHelpers

VIEW = "a" * 32
KEY = "AAAAAAAAAAAAAAAAAAAAAA=="
UPGRADE = {
    "Upgrade": "websocket",
    "Connection": "keep-alive, Upgrade",
    "Sec-WebSocket-Version": "13",
    "Sec-WebSocket-Key": KEY,
    "Sec-WebSocket-Protocol": "binary",
}


def headers(values: dict[str, str], *extra: tuple[str, str]) -> Message:
    message = Message()
    for name, value in values.items():
        message[name] = value
    for name, value in extra:
        message[name] = value
    return message


class FakeService:
    def __init__(self, *, error=None, busy=False, upgrade=True, after=None):
        self.error, self.busy, self.upgrade, self.after = error, busy, upgrade, after
        self.carried = []

    def open_view(self, team_id):
        if self.error is not None:
            raise self.error
        return VIEW

    @contextmanager
    def view_slot(self):
        if self.busy:
            raise BrowserError(view_protocol.BUSY)
        yield

    def carry_view(self, team_id, view_id, client, key, upgraded):
        self.carried.append((team_id, view_id, client, key))
        if self.error is not None:
            raise self.error
        if self.upgrade:
            upgraded(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
        if self.after is not None:
            raise self.after


class BrowserStreamHttpTests(LocalHttpEdgeHelpers, unittest.TestCase):
    def stream(self, service, *, view_id=VIEW, request_headers=None):
        handler = self.handler(controller=SimpleNamespace(browser_service=service))
        handler.headers = request_headers if request_headers is not None else headers(UPGRADE)
        handler.connection = mock.sentinel.connection
        audit = mock.Mock()
        handler._session_route(self.route("browser-view-stream", team_id="team_1", view_id=view_id), audit)
        return handler, audit

    def refused(self, service, **arguments):
        with self.assertRaises(ApiProblemError) as raised:
            self.stream(service, **arguments)
        return raised.exception

    def test_an_exact_upgrade_is_carried_and_its_101_written(self):
        service = FakeService()
        handler, audit = self.stream(service)
        self.assertEqual(service.carried, [("team_1", VIEW, mock.sentinel.connection, KEY)])
        self.assertEqual(handler.wfile.getvalue(), b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
        self.assertTrue(handler.close_connection)
        audit.record.assert_called_once_with("browser-view-stream", result="ok", team_id="team_1", detail="upgraded")

    def test_a_failure_after_the_upgrade_only_ends_the_stream(self):
        handler, _audit = self.stream(FakeService(after=BrowserError(view_protocol.UNAVAILABLE)))
        self.assertTrue(handler.close_connection)

    def test_anything_but_the_exact_upgrade_is_refused_before_any_carrying(self):
        broken = (
            {**UPGRADE, "Upgrade": "h2c"},
            {**UPGRADE, "Connection": "keep-alive"},
            {**UPGRADE, "Sec-WebSocket-Version": "8"},
            {**UPGRADE, "Sec-WebSocket-Protocol": "base64"},
            {**UPGRADE, "Sec-WebSocket-Key": "AAAAAAAAAAAAAAAAAAAAAB=="},
            {name: value for name, value in UPGRADE.items() if name != "Sec-WebSocket-Key"},
        )
        for values in broken:
            with self.subTest(values=values):
                service = FakeService()
                problem = self.refused(service, request_headers=headers(values))
                self.assertEqual(
                    (problem.status, problem.code), (HTTPStatus.BAD_REQUEST, view_protocol.INVALID_UPGRADE)
                )
                self.assertEqual(service.carried, [])
        for extra in (("Sec-WebSocket-Extensions", "permessage-deflate"), ("Sec-WebSocket-Key", KEY)):
            with self.subTest(extra=extra):
                problem = self.refused(FakeService(), request_headers=headers(UPGRADE, extra))
                self.assertEqual(problem.code, view_protocol.INVALID_UPGRADE)

    def test_each_refusal_before_the_upgrade_is_closed(self):
        cases = (
            ({"view_id": VIEW.upper()}, FakeService(), HTTPStatus.NOT_FOUND, view_protocol.NOT_RUNNING),
            ({}, FakeService(busy=True), HTTPStatus.SERVICE_UNAVAILABLE, view_protocol.BUSY),
            (
                {},
                FakeService(error=BrowserError(view_protocol.NOT_RUNNING)),
                HTTPStatus.NOT_FOUND,
                view_protocol.NOT_RUNNING,
            ),
            (
                {},
                FakeService(error=BrowserError("browser-lost")),
                HTTPStatus.SERVICE_UNAVAILABLE,
                view_protocol.UNAVAILABLE,
            ),
            (
                {},
                FakeService(upgrade=False, after=BrowserError(view_protocol.UNAVAILABLE)),
                HTTPStatus.SERVICE_UNAVAILABLE,
                view_protocol.UNAVAILABLE,
            ),
        )
        for arguments, service, status, code in cases:
            with self.subTest(code=code, arguments=arguments):
                problem = self.refused(service, **arguments)
                self.assertEqual((problem.status, problem.code), (status, code))

    def test_opening_answers_the_running_tasks_view(self):
        operation = server._OPERATIONS["browser-view-open"]
        route = self.route("browser-view-open", team_id="team_1")
        handler = self.handler("POST", controller=SimpleNamespace(browser_service=FakeService()))
        self.assertEqual(
            operation(handler, route), (HTTPStatus.OK, {"view_id": VIEW}, "browser-view-open", "team_1", None)
        )
        handler.server.controller.browser_service.error = BrowserError(view_protocol.NOT_RUNNING)
        with self.assertRaises(ApiProblemError) as raised:
            operation(handler, route)
        self.assertEqual(raised.exception.code, view_protocol.NOT_RUNNING)
        self.assertIs(local_http_browser.OPERATIONS["browser-view-open"], operation)


if __name__ == "__main__":
    unittest.main()
