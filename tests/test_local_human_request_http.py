from __future__ import annotations

import unittest
from http import HTTPStatus
from types import SimpleNamespace

from core.http import strict as strict_http
from local.http import server


class LocalHumanRequestHttpTests(unittest.TestCase):
    def test_pending_and_submit_routes_are_exact_and_use_streaming_resume(self) -> None:
        pending = {"team_id": "team_1", "status": "human-required"}
        completed = {"team_id": "team_1", "team_name": "Team One", "reply": "Done"}
        service = SimpleNamespace(
            pending_chat_human=lambda team_id: pending,
            resume_chat_human=lambda team_id, body, provider, api_key, progress: completed,
        )
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(controller=SimpleNamespace(chat_turn_service=service))
        handler._model_credential_headers = lambda: ("openai", "test-api-key")
        handler._body = lambda **_kwargs: {
            "challenge_id": "a" * 32,
            "decision": "submit",
            "value": True,
        }

        handler.command = "GET"
        self.assertEqual(
            handler._chat_route(["v1", "teams", "team_1", "chat", "human"]),
            (HTTPStatus.OK, pending, "chat-human-pending", "team_1", None),
        )
        handler.command = "POST"
        self.assertEqual(
            handler._chat_route(["v1", "teams", "team_1", "chat", "human"]),
            (HTTPStatus.OK, completed, "chat-human-submit", "team_1", None),
        )
        self.assertEqual(handler._chat_status(pending), HTTPStatus.PRECONDITION_REQUIRED)

    def test_human_routes_are_local_supervisor_authority_in_this_delivery(self) -> None:
        local_get = strict_http.resolve_controller_route(
            strict_http.LOCAL_CONTROLLER,
            "GET",
            ("v1", "teams", "team_1", "chat", "human"),
        )
        local_post = strict_http.resolve_controller_route(
            strict_http.LOCAL_CONTROLLER,
            "POST",
            ("v1", "teams", "team_1", "chat", "human"),
        )
        hosted = strict_http.resolve_controller_route(
            strict_http.HOSTED_CONTROLLER,
            "POST",
            ("v1", "teams", "team_1", "chat", "human"),
        )

        self.assertEqual(local_get.operation, "chat-human-pending")
        self.assertEqual(local_post.operation, "chat-human-submit")
        self.assertIsNone(hosted)


if __name__ == "__main__":
    unittest.main()
