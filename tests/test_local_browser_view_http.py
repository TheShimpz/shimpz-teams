"""The Browser screen view over HTTP: the open route and the frame route's exact answers (ADR-0117, M2 Watch)."""

import unittest
from contextlib import contextmanager
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from local.browser import view as local_view
from local.browser.runtime import BrowserError
from local.errors import ApiProblemError
from local.http import browser as local_http_browser
from local.http import server
from protocol.http.v1 import browser_view as view_protocol
from tests.test_local_http_server_edges import LocalHttpEdgeHelpers

VIEW = "a" * 32
PNG = view_protocol.PNG_SIGNATURE + b"frame"


class FakeService:
    def __init__(self, *, frame=None, error=None, busy=False):
        self.frame, self.error, self.busy = frame, error, busy
        self.asked = []

    def open_view(self, team_id):
        if self.error is not None:
            raise self.error
        return VIEW

    @contextmanager
    def view_slot(self):
        if self.busy:
            raise BrowserError(view_protocol.BUSY)
        yield

    def view_frame(self, team_id, view_id, after):
        self.asked.append((team_id, view_id, after))
        if self.error is not None:
            raise self.error
        return self.frame


class BrowserViewHttpTests(LocalHttpEdgeHelpers, unittest.TestCase):
    def frame(self, service, *, view_id=VIEW, after="0"):
        handler = self.handler(controller=SimpleNamespace(browser_service=service))
        audit = mock.Mock()
        route = self.route("browser-view-frame", team_id="team_1", view_id=view_id, after=after)
        handler._session_route(route, audit)
        return handler, audit

    def refused(self, service, **path):
        with self.assertRaises(ApiProblemError) as raised:
            self.frame(service, **path)
        return raised.exception

    def test_a_frame_is_one_png_numbered_in_its_header(self):
        service = FakeService(frame=local_view.Frame(3, PNG))
        handler, audit = self.frame(service, after="2")
        self.assertEqual(service.asked, [("team_1", VIEW, 2)])
        handler.send_response.assert_called_once_with(HTTPStatus.OK)
        headers = {call.args[0]: call.args[1] for call in handler.send_header.call_args_list}
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Content-Length"], str(len(PNG)))
        self.assertEqual(headers[view_protocol.FRAME_HEADER], "3")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(handler.wfile.getvalue(), PNG)
        audit.record.assert_called_once_with("browser-view-frame", result="ok", team_id="team_1", detail="frame=3")

    def test_no_newer_frame_is_an_empty_answer(self):
        handler, audit = self.frame(FakeService())
        handler.send_response.assert_called_once_with(HTTPStatus.NO_CONTENT)
        self.assertEqual(handler.wfile.getvalue(), b"")
        audit.record.assert_called_once_with("browser-view-frame", result="ok", team_id="team_1", detail="unchanged")

    def test_a_path_that_names_no_view_or_cursor_is_not_running(self):
        for path in ({"view_id": VIEW.upper()}, {"view_id": "a" * 31}, {"after": "01"}, {"after": "-1"}):
            with self.subTest(path=path):
                problem = self.refused(FakeService(), **path)
                self.assertEqual((problem.status, problem.code), (HTTPStatus.NOT_FOUND, view_protocol.NOT_RUNNING))

    def test_each_refusal_is_closed(self):
        cases = (
            (FakeService(busy=True), HTTPStatus.SERVICE_UNAVAILABLE, view_protocol.BUSY),
            (
                FakeService(error=BrowserError(view_protocol.NOT_RUNNING)),
                HTTPStatus.NOT_FOUND,
                view_protocol.NOT_RUNNING,
            ),
            (
                FakeService(error=BrowserError("browser-lost")),
                HTTPStatus.SERVICE_UNAVAILABLE,
                view_protocol.UNAVAILABLE,
            ),
            # Team never sends a frame outside its framing.
            (
                FakeService(frame=local_view.Frame(1, b"GIF89a")),
                HTTPStatus.SERVICE_UNAVAILABLE,
                view_protocol.UNAVAILABLE,
            ),
            (FakeService(frame=local_view.Frame(0, PNG)), HTTPStatus.SERVICE_UNAVAILABLE, view_protocol.UNAVAILABLE),
        )
        for service, status, code in cases:
            with self.subTest(code=code):
                problem = self.refused(service)
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
