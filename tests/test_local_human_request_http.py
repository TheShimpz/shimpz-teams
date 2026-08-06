from __future__ import annotations

import json
import unittest
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from core.http import strict as strict_http
from local.http import server


class LocalHumanRequestHttpTests(unittest.TestCase):
    @staticmethod
    def _json_headers(length: int) -> Message:
        headers = Message()
        headers["Content-Length"] = str(length)
        headers["Content-Type"] = "application/json"
        return headers

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

    def test_human_routes_are_shared_with_profile_specific_authority(self) -> None:
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
        self.assertEqual(hosted.operation, "chat-human-submit")

    def test_human_response_body_accepts_the_maximum_unicode_textarea(self) -> None:
        value = "\U0001f9e0" * 16_000
        raw = json.dumps(
            {
                "challenge_id": "a" * 32,
                "decision": "submit",
                "value": value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

        captured, body = strict_http.read_json_document(
            self._json_headers(len(raw)),
            BytesIO(raw),
            max_bytes=server.MAX_HUMAN_RESPONSE_BODY_BYTES,
        )

        self.assertEqual(server.MAX_HUMAN_RESPONSE_BODY_BYTES, 128 * 1024)
        self.assertEqual(captured, raw)
        self.assertEqual(body["value"], value)

    def test_oversized_human_response_is_rejected_before_the_body_is_read(self) -> None:
        stream = mock.Mock()

        with self.assertRaises(strict_http.HttpContractError) as caught:
            strict_http.read_json_document(
                self._json_headers(server.MAX_HUMAN_RESPONSE_BODY_BYTES + 1),
                stream,
                max_bytes=server.MAX_HUMAN_RESPONSE_BODY_BYTES,
            )

        self.assertEqual(caught.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        stream.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
