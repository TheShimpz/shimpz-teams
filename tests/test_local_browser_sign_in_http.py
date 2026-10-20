"""A Team's Sign-in over HTTP: starting, saving, and cancelling a sign-in session, reading and deleting the Sign-in."""

import unittest
from http import HTTPStatus
from types import SimpleNamespace

from local.browser import sign_in as local_sign_in
from local.browser import sign_ins as sign_in_store
from local.errors import ApiProblemError
from local.http import server
from tests.test_local_http_server_edges import LocalHttpEdgeHelpers

TEAM = "team_1"
SESSION = "a" * 32
SIGN_IN = "b" * 32
INCARNATION = "c" * 64
STARTED = {"sign_in_session_id": SESSION, "view_id": "c" * 32, "site": "www.linkedin.com"}
SIGN_IN_VIEW = {
    "sign_in_id": SIGN_IN,
    "state": "ready",
    "generation": 1,
    "team_incarnation": INCARNATION,
    "sites": ["linkedin.com"],
    "sites_truncated": False,
    "created_at": "2026-10-19T12:00:00Z",
    "saved_at": "2026-10-19T12:00:00Z",
    "last_used_at": "2026-10-19T12:00:00Z",
    "expires_at": "2026-10-26T12:00:00Z",
}
SAVED = {"sign_in": SIGN_IN_VIEW, "recorded": True}
CONFIRMED = {"sign_in_id": SIGN_IN, "generation": 1, "team_incarnation": INCARNATION, "factor": "totp"}


class FakeSessions:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def _answer(self, call, answer):
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return answer

    def start(self, team_id, incarnation, url, locale):
        return self._answer(("start", team_id, incarnation(), url, locale), STARTED)

    def save(self, team_id, session_id, incarnation):
        return self._answer(("save", team_id, session_id, incarnation()), SAVED)

    def cancel(self, team_id, session_id):
        return self._answer(("cancel", team_id, session_id), None)

    def delete(self, team_id, confirmed):
        return self._answer(("delete", team_id, confirmed), "deleted")


class FakeStore:
    def __init__(self, error=None):
        self.error = error

    def current(self, team_id):
        if self.error is not None:
            raise self.error
        return SIGN_IN_VIEW


class SignInHttpTests(LocalHttpEdgeHelpers, unittest.TestCase):
    def call(self, operation, body=None, *, sessions=None, store=None, network=None):
        def network_of(team_id, *, required=True):
            if network is not None:
                raise network
            return SimpleNamespace(id=INCARNATION)

        controller = SimpleNamespace(
            sign_in_sessions=sessions or FakeSessions(),
            sign_in_store=store or FakeStore(),
            assistant_lifecycle=SimpleNamespace(_network=network_of),
        )
        handler = self.handler("POST", controller=controller)
        if body is not None:
            handler._captured_json_raw = b"{}"
            handler._captured_json_body = body
        status, answer, _operation, team_id, _extra = server._OPERATIONS[operation](
            handler, self.route(operation, team_id=TEAM)
        )
        self.assertEqual((status, team_id), (HTTPStatus.OK, TEAM))
        return answer, controller

    def refused(self, operation, body=None, **arguments):
        with self.assertRaises(ApiProblemError) as raised:
            self.call(operation, body, **arguments)
        return raised.exception

    def test_a_session_starts_saves_and_cancels_with_the_teams_current_incarnation(self):
        started, controller = self.call(
            "browser-sign-in-start", {"url": "https://www.linkedin.com/login", "locale": "pt"}
        )
        self.assertEqual(started, STARTED)
        self.assertEqual(
            controller.sign_in_sessions.calls,
            [("start", TEAM, INCARNATION, "https://www.linkedin.com/login", "pt")],
        )
        saved, controller = self.call("browser-sign-in-save", {"sign_in_session_id": SESSION})
        self.assertEqual(saved, SAVED)
        self.assertEqual(controller.sign_in_sessions.calls, [("save", TEAM, SESSION, INCARNATION)])
        cancelled, _controller = self.call("browser-sign-in-cancel", {"sign_in_session_id": SESSION})
        self.assertEqual(cancelled, {"cancelled": True})

    def test_a_save_whose_team_is_gone_sees_no_incarnation(self):
        sessions = FakeSessions()
        self.call(
            "browser-sign-in-save",
            {"sign_in_session_id": SESSION},
            sessions=sessions,
            network=ApiProblemError(HTTPStatus.NOT_FOUND, "gone", code="team-not-found"),
        )
        self.assertEqual(sessions.calls, [("save", TEAM, SESSION, None)])
        # A start asks for the Team as it is before any Browser starts.
        gone = self.refused(
            "browser-sign-in-start",
            {"url": "https://www.linkedin.com/login", "locale": "pt"},
            network=ApiProblemError(HTTPStatus.NOT_FOUND, "gone", code="team-not-found"),
        )
        self.assertEqual(gone.code, "team-not-found")

    def test_the_sign_in_is_read_and_deleted_as_confirmed(self):
        current, _controller = self.call("browser-sign-in")
        self.assertEqual(current, {"sign_in": SIGN_IN_VIEW})
        deleted, controller = self.call("browser-sign-in-delete", CONFIRMED)
        self.assertEqual(deleted, {"deletion": "deleted"})
        self.assertEqual(controller.sign_in_sessions.calls, [("delete", TEAM, CONFIRMED)])

    def test_every_body_outside_its_exact_form_is_refused(self):
        for operation, body in (
            ("browser-sign-in-start", {"url": "http://example.com/", "locale": "pt"}),
            ("browser-sign-in-save", {"sign_in_session_id": SESSION, "label": "Work"}),
            ("browser-sign-in-cancel", {"sign_in_session_id": "x"}),
            ("browser-sign-in-delete", {**CONFIRMED, "factor": "password"}),
        ):
            with self.subTest(operation=operation):
                problem = self.refused(operation, body)
                self.assertEqual((problem.status, problem.code), (HTTPStatus.BAD_REQUEST, "invalid-body"))

    def test_each_session_refusal_has_its_closed_answer(self):
        for code, status in (
            ("sign-in-not-running", HTTPStatus.NOT_FOUND),
            ("sign-in-windows", HTTPStatus.CONFLICT),
            ("sign-in-identity-provider", HTTPStatus.CONFLICT),
            ("sign-in-unsaved", HTTPStatus.SERVICE_UNAVAILABLE),
            ("browser-busy", HTTPStatus.CONFLICT),
            ("sign-in-busy", HTTPStatus.CONFLICT),
            ("sign-in-changed", HTTPStatus.CONFLICT),
            ("sign-in-absent", HTTPStatus.NOT_FOUND),
            ("team-not-found", HTTPStatus.NOT_FOUND),
            ("anything-else", HTTPStatus.SERVICE_UNAVAILABLE),
        ):
            with self.subTest(code=code):
                sessions = FakeSessions(local_sign_in.SignInSessionError(code))
                problem = self.refused("browser-sign-in-cancel", {"sign_in_session_id": SESSION}, sessions=sessions)
                expected = code if code != "anything-else" else "browser-unavailable"
                self.assertEqual((problem.status, problem.code), (status, expected))
        for operation, body in (
            ("browser-sign-in-start", {"url": "https://a.com/", "locale": "en"}),
            ("browser-sign-in-save", {"sign_in_session_id": SESSION}),
            ("browser-sign-in-delete", CONFIRMED),
        ):
            with self.subTest(operation=operation):
                sessions = FakeSessions(local_sign_in.SignInSessionError("sign-in-unsaved"))
                self.assertEqual(self.refused(operation, body, sessions=sessions).code, "sign-in-unsaved")

    def test_an_unreadable_sign_in_is_unavailable(self):
        problem = self.refused("browser-sign-in", store=FakeStore(sign_in_store.SignInError("unreadable")))
        self.assertEqual((problem.status, problem.code), (HTTPStatus.SERVICE_UNAVAILABLE, "sign-ins-unavailable"))


if __name__ == "__main__":
    unittest.main()
