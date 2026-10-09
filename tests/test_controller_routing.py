"""Exact method and path contracts for the Controller router."""

import sys
import unittest
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from core.http import strict as strict_http


def _parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


class ControllerRoutingTests(unittest.TestCase):
    def test_routes_resolve_to_their_operation_and_parameters(self) -> None:
        common = (
            ("GET", "/v1/teams", "team-list", {}),
            ("POST", "/v1/teams/team_1/chat", "chat", {"team_id": "team_1"}),
            (
                "POST",
                "/v1/teams/team_1/chat/human",
                "chat-human-submit",
                {"team_id": "team_1"},
            ),
            (
                "POST",
                "/v1/teams/team_1/assistant-integrations/challenges/challenge-1/authorize",
                "assistant-integration-authorize",
                {"team_id": "team_1", "challenge_id": "challenge-1"},
            ),
            (
                "GET",
                "/v1/teams/team_1/assistant-stored-inputs",
                "assistant-stored-input-list",
                {"team_id": "team_1"},
            ),
            (
                "DELETE",
                "/v1/teams/team_1/assistant-stored-inputs/whatsapp/whatsapp-token",
                "assistant-stored-input-clear",
                {
                    "team_id": "team_1",
                    "assistant_id": "whatsapp",
                    "stored_input_id": "whatsapp-token",
                },
            ),
            (
                "GET",
                "/v1/teams/team_1/assistants/helper/summary/pt",
                "assistant-summary",
                {"team_id": "team_1", "assistant_id": "helper", "locale": "pt"},
            ),
            (
                "GET",
                "/v1/teams/team_1/assistants/helper/details/pt",
                "assistant-details",
                {"team_id": "team_1", "assistant_id": "helper", "locale": "pt"},
            ),
        )
        for method, path, operation, params in common:
            with self.subTest(method=method, path=path):
                self.assertEqual(
                    strict_http.resolve_controller_route(method, _parts(path)),
                    strict_http.ControllerRouteMatch(operation, params),
                )

    def test_removed_assistant_help_routes_do_not_resolve(self) -> None:
        paths = (
            "/v1/teams/team_1/assistants/helper/help",
            "/v1/teams/team_1/assistants/helper/help/pt-BR",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertIsNone(strict_http.resolve_controller_route("GET", _parts(path)))

    def test_local_routes_resolve_to_their_operation(self) -> None:
        cases = (
            (
                "DELETE",
                "/v1/teams/team_1/assistant-integrations/challenges/challenge-1/authorize",
                "assistant-integration-cancel",
            ),
            (
                "DELETE",
                "/v1/space/bootstrap",
                "space-bootstrap-reset",
            ),
            (
                "GET",
                "/v1/local-assistants",
                "local-assistant-list",
            ),
            (
                "GET",
                "/v1/local-assistants/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/icon",
                "local-assistant-icon",
            ),
            (
                "GET",
                "/v1/local-assistants/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/summary/pt",
                "local-assistant-summary",
            ),
            (
                "GET",
                "/v1/local-assistants/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/details/pt",
                "local-assistant-details",
            ),
            (
                "POST",
                "/v1/teams/team_1/assistants/local",
                "local-assistant-install",
            ),
            (
                "POST",
                "/v1/teams/team_1/assistants/local/fresh",
                "local-assistant-fresh-install",
            ),
        )
        for method, path, operation in cases:
            with self.subTest(path=path):
                self.assertEqual(strict_http.resolve_controller_route(method, _parts(path)).operation, operation)

    def test_wrong_methods_and_suffixes_do_not_fall_through(self) -> None:
        self.assertIsNone(strict_http.resolve_controller_route("GET", _parts("/v1/teams/t/chat")))
        self.assertIsNone(
            strict_http.resolve_controller_route(
                "POST",
                _parts("/v1/teams/t/files/id/extra"),
            )
        )
        fresh_path = "/v1/teams/team_1/assistants/local/fresh"
        self.assertIsNone(
            strict_http.resolve_controller_route(
                "GET",
                _parts(fresh_path),
            )
        )
        self.assertIsNone(
            strict_http.resolve_controller_route(
                "POST",
                _parts(f"{fresh_path}/extra"),
            )
        )
        action_labels = strict_http.resolve_controller_route(
            "POST",
            _parts("/v1/teams/team_1/assistants/local/action-labels"),
        )
        self.assertEqual(action_labels.operation, "assistant-action-labels")
        self.assertEqual(action_labels.params["assistant_id"], "local")


if __name__ == "__main__":
    unittest.main()
