"""Route and streaming edge coverage for the Hosted HTTP controller."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

server = harness.hosted_controller
assistant_lifecycle = harness.assistant_lifecycle
hosted_chat_api = server.hosted_chat_api
hosted_lifecycle = server.hosted_lifecycle
hosted_resources = harness.hosted_resources
runtime_state = harness.runtime_state

TEAM_ID = "team_1"
ACCOUNT_ID = "a" * 32
SOURCE_DIGEST = f"sha256:{'a' * 64}"
OCI_DIGEST = f"sha256:{'b' * 64}"


def _handler() -> server.Handler:
    handler = object.__new__(server.Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = mock.Mock()
    handler.send_header = mock.Mock()
    handler.end_headers = mock.Mock()
    handler._send_json = mock.Mock()
    handler._send_icon = mock.Mock()
    handler._audit_security = mock.Mock(return_value="trace")
    return handler


def _request(**params: str) -> server._AuthorizedRequest:
    lease = SimpleNamespace(owner=ACCOUNT_ID, container_id="c" * 64)
    return server._AuthorizedRequest(
        params,
        TEAM_ID,
        ("account", ACCOUNT_ID),
        lease,
        {},
        {"kind": "password"},
    )


@contextlib.contextmanager
def _exclusive_turn(_team_id, _lease):
    yield "turn-token", SimpleNamespace(id="c" * 64)


class HostedHttpStreamEdgeTests(unittest.TestCase):
    def test_log_and_standard_dispatch_paths_are_exercised(self) -> None:
        handler = _handler()
        self.assertIsNone(handler.log_message("ignored"))
        handler._send_json = server.Handler._send_json.__get__(handler)
        handler._send_json(HTTPStatus.OK, {"ok": True})
        self.assertNotIn(mock.call("Cache-Control", "no-store"), handler.send_header.call_args_list)

        handler.path = "/v1/teams"
        with (
            mock.patch.object(server.developers_http, "is_path", return_value=False),
            mock.patch.object(server.stdlib, "dispatch") as dispatch,
        ):
            handler._dispatch("GET")
        dispatch.assert_called_once()

    def test_stream_rejects_a_turn_when_a_durable_continuation_is_pending(self) -> None:
        handler = _handler()
        pending = {"status": "integrations-required"}
        with (
            mock.patch.object(hosted_chat_api, "_exclusive_chat_turn", _exclusive_turn),
            mock.patch.object(hosted_chat_api, "_pending_hosted_chat", return_value=pending),
        ):
            handler._stream_chat(TEAM_ID, "hello", [], (), _request().lease)
        handler._send_json.assert_called_once_with(HTTPStatus.PRECONDITION_REQUIRED, pending, no_store=True)

    def test_stream_exposes_paused_terminal_status_and_audits_it(self) -> None:
        handler = _handler()
        handler._send_json = server.Handler._send_json.__get__(handler)
        paused_status = "human-required"
        result = {"status": paused_status, "team_id": TEAM_ID}
        with (
            mock.patch.object(hosted_chat_api, "_exclusive_chat_turn", _exclusive_turn),
            mock.patch.object(hosted_chat_api, "_pending_hosted_chat", return_value=None),
            mock.patch.object(server.hosted_chat_segment, "_chat_in_turn", return_value=result),
        ):
            handler._stream_chat(TEAM_ID, "hello", [], (), _request().lease)
        self.assertIn(paused_status.encode(), handler.wfile.getvalue())
        handler._audit_security.assert_called_once_with(
            "chat",
            TEAM_ID,
            result="ok",
            streamed=True,
            status=paused_status,
            reason=None,
        )

    def test_stream_maps_controlled_errors_without_leaking_exception_details(self) -> None:
        cases = (
            (runtime_state.ApiError(HTTPStatus.CONFLICT, "brain turn stopped"), b'"type": "stopped"'),
            (runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "invalid"), b'"detail": "invalid"'),
        )
        for error, expected in cases:
            with self.subTest(error=error.message):
                handler = _handler()
                handler._send_json = server.Handler._send_json.__get__(handler)
                with (
                    mock.patch.object(hosted_chat_api, "_exclusive_chat_turn", _exclusive_turn),
                    mock.patch.object(hosted_chat_api, "_pending_hosted_chat", return_value=None),
                    mock.patch.object(server.hosted_chat_segment, "_chat_in_turn", side_effect=error),
                ):
                    handler._stream_chat(TEAM_ID, "hello", [], (), _request().lease)
                self.assertIn(expected, handler.wfile.getvalue())

    def test_stream_redacts_infrastructure_failures(self) -> None:
        handler = _handler()
        handler._send_json = server.Handler._send_json.__get__(handler)
        error = server.docker.errors.DockerException("secret transport detail")
        with (
            mock.patch.object(hosted_chat_api, "_exclusive_chat_turn", _exclusive_turn),
            mock.patch.object(hosted_chat_api, "_pending_hosted_chat", return_value=None),
            mock.patch.object(server.hosted_chat_segment, "_chat_in_turn", side_effect=error),
        ):
            handler._stream_chat(TEAM_ID, "hello", [], (), _request().lease)
        body = handler.wfile.getvalue()
        self.assertIn(b"brain stream failed", body)
        self.assertNotIn(b"secret transport detail", body)


class HostedHttpSimpleRouteEdgeTests(unittest.TestCase):
    def test_team_list_scopes_accounts_but_not_supervisors(self) -> None:
        handler = _handler()
        with mock.patch.object(hosted_lifecycle, "_list", return_value={"teams": []}) as list_teams:
            handler._route_team_list(("account", ACCOUNT_ID))
            handler._route_team_list(("supervisor", None))
        self.assertEqual(list_teams.call_args_list, [mock.call(owner=ACCOUNT_ID), mock.call(owner=None)])

    def test_oauth_completion_is_audited_and_never_cached(self) -> None:
        handler = _handler()
        handler._read_body = mock.Mock(return_value={"code": "claim"})
        result = {
            "team_id": TEAM_ID,
            "assistant_id": "cloudflare",
            "provider": "cloudflare",
        }
        with mock.patch.object(hosted_chat_api, "_complete_oauth_integration", return_value=(result, ACCOUNT_ID)):
            handler._route_assistant_integration_complete()
        handler._send_json.assert_called_once_with(HTTPStatus.OK, result, no_store=True)
        handler._audit_security.assert_called_once()

    def test_team_create_requires_owner_and_returns_a_trace(self) -> None:
        handler = _handler()
        with self.assertRaises(runtime_state.ApiError):
            handler._route_team_create(TEAM_ID, ("account", ACCOUNT_ID), None)

        handler._read_body = mock.Mock(return_value={"team_name": "Marketing", "owner_account_id": ACCOUNT_ID})
        with (
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_lifecycle, "_create", return_value={"created": True}) as create,
        ):
            handler._route_team_create(TEAM_ID, ("account", ACCOUNT_ID), ACCOUNT_ID)
        create.assert_called_once_with(TEAM_ID, {"team_name": "Marketing"}, ACCOUNT_ID)
        handler._send_json.assert_called_once_with(HTTPStatus.OK, {"created": True, "trace_id": "trace"})

    def test_team_create_reuses_its_dedicated_body_limit_after_authorization(self) -> None:
        handler = _handler()
        body = {"team_name": "M" * 2048, "owner_account_id": ACCOUNT_ID}
        handler._captured_json_body = body
        handler._captured_json_raw = json.dumps(body).encode()
        with (
            mock.patch.object(runtime_state, "MAX_JSON_BODY_BYTES", 1024),
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_lifecycle, "_create", return_value={"created": True}) as create,
        ):
            handler._route_team_create(TEAM_ID, ("account", ACCOUNT_ID), ACCOUNT_ID)
        create.assert_called_once_with(TEAM_ID, {"team_name": "M" * 2048}, ACCOUNT_ID)

    def test_team_destroy_uses_the_cleanup_authorization_successor(self) -> None:
        handler = _handler()
        lease = mock.sentinel.lease
        with (
            mock.patch.object(hosted_resources, "_authorize_destroy", return_value=lease),
            mock.patch.object(hosted_lifecycle, "_destroy", return_value={"db_dropped": True}) as destroy,
        ):
            handler._route_team_destroy(TEAM_ID, ("account", ACCOUNT_ID), ACCOUNT_ID)
        destroy.assert_called_once_with(TEAM_ID, lease)
        handler._send_json.assert_called_once_with(
            HTTPStatus.OK,
            {"db_dropped": True, "trace_id": "trace"},
        )

    def test_integration_routes_cover_inventory_authorize_and_disconnect(self) -> None:
        handler = _handler()
        request = _request(
            challenge_id="d" * 32,
            assistant_id="cloudflare",
            integration_id="oauth",
        )
        with mock.patch.object(
            server.hosted_assistants,
            "_assistant_integration_inventory",
            return_value={"integrations": []},
        ):
            handler._route_assistant_integration_list(request)

        handler._read_body = mock.Mock(return_value={"invalid": True})
        with self.assertRaises(runtime_state.ApiError):
            handler._route_assistant_integration_authorize(request)
        handler._read_body = mock.Mock(
            return_value={
                "assistant_id": "cloudflare",
                "integration_id": "oauth",
                "session_binding": "browser",
            }
        )
        with mock.patch.object(hosted_chat_api, "_start_oauth_integration", return_value={"url": "https://oauth"}):
            handler._route_assistant_integration_authorize(request)

        with mock.patch.object(
            hosted_chat_api,
            "_disconnect_oauth_integration",
            return_value={"disconnected": True},
        ):
            handler._route_assistant_integration_disconnect(request)
        self.assertEqual(handler._send_json.call_count, 3)

    def test_team_observation_and_lifecycle_routes_delegate_exactly(self) -> None:
        handler = _handler()
        request = _request()
        request = server._AuthorizedRequest(
            request.params,
            request.team_id,
            request.principal,
            request.lease,
            {"lines": "25"},
            request.assurance,
        )
        with (
            mock.patch.object(hosted_lifecycle, "_status", return_value={"status": "running"}),
            mock.patch.object(hosted_lifecycle, "_logs", return_value={"logs": []}) as logs,
            mock.patch.object(hosted_lifecycle, "_lifecycle", return_value={"status": "stopped"}) as lifecycle,
        ):
            handler._route_team_status(request)
            handler._route_team_logs(request)
            handler._route_team_lifecycle(request, operation="stop")
        logs.assert_called_once_with(TEAM_ID, 25, request.lease)
        lifecycle.assert_called_once_with(TEAM_ID, "stop", request.lease)

    def test_file_routes_bound_storage_and_release_the_global_upload_slot(self) -> None:
        handler = _handler()
        request = _request(file_id="e" * 32)
        with mock.patch.object(hosted_lifecycle, "_list_team_files", return_value={"files": []}):
            handler._route_file_list(request)

        denied_slot = mock.Mock()
        denied_slot.acquire.return_value = False
        with (
            mock.patch.object(runtime_state, "_file_upload_slots", denied_slot),
            self.assertRaises(runtime_state.ApiError),
        ):
            handler._route_file_upload(request)

        handler._read_file_body = mock.Mock(return_value=("brief.txt", b"body", "text/plain"))
        accepted_slot = mock.Mock()
        accepted_slot.acquire.return_value = True
        stored = {"file": {"id": "e" * 32, "size": 4}}
        with (
            mock.patch.object(runtime_state, "_file_upload_slots", accepted_slot),
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_lifecycle, "_put_inbox_file", return_value=stored),
        ):
            handler._route_file_upload(request)
        accepted_slot.release.assert_called_once()

        with mock.patch.object(
            hosted_lifecycle,
            "_delete_team_file",
            return_value={"id": "e" * 32, "deleted": True},
        ):
            handler._route_file_delete(request)
        self.assertEqual(handler._send_json.call_count, 3)

    def test_inference_routes_report_and_configure_the_selected_model(self) -> None:
        handler = _handler()
        handler._read_body = mock.Mock(return_value={"provider": "openai", "model": "gpt"})
        request = _request()
        with (
            mock.patch.object(hosted_lifecycle, "_inference_status", return_value={"configured": True}),
            mock.patch.object(
                hosted_lifecycle,
                "_configure_inference",
                return_value={"provider": "openai", "model": "gpt"},
            ),
        ):
            handler._route_inference_status(request)
            handler._route_inference_configure(request)
        self.assertEqual(handler._send_json.call_count, 2)


class HostedHttpChatRouteEdgeTests(unittest.TestCase):
    def test_chat_rejects_an_open_or_incomplete_body(self) -> None:
        handler = _handler()
        handler._read_body = mock.Mock(return_value={"message": "hello"})
        with self.assertRaises(runtime_state.ApiError):
            handler._route_chat_turn(_request(), stream=False)

    def test_stream_checks_pending_state_before_starting_transport(self) -> None:
        handler = _handler()
        handler._read_body = mock.Mock(return_value={"message": "hello", "files": [], "assistant_ids": []})
        pending = {"status": "input-required"}
        with (
            mock.patch.object(server.validate, "validate_chat_message", return_value="hello"),
            mock.patch.object(server.hosted_assistants, "_chat_assistant_ids", return_value=()),
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_chat_api, "_pending_hosted_chat", return_value=pending),
        ):
            handler._route_chat_turn(_request(), stream=True)
        handler._send_json.assert_called_once_with(HTTPStatus.PRECONDITION_REQUIRED, pending, no_store=True)

    def test_stream_delegates_validated_inputs_when_no_continuation_is_pending(self) -> None:
        handler = _handler()
        handler._read_body = mock.Mock(
            return_value={"message": "hello", "files": ["file"], "assistant_ids": ["assistant"]}
        )
        handler._stream_chat = mock.Mock()
        request = _request()
        with (
            mock.patch.object(server.validate, "validate_chat_message", return_value="hello"),
            mock.patch.object(server.hosted_assistants, "_chat_assistant_ids", return_value=("assistant",)),
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_chat_api, "_pending_hosted_chat", return_value=None),
        ):
            handler._route_chat_turn(request, stream=True)
        handler._stream_chat.assert_called_once_with(
            TEAM_ID,
            "hello",
            ["file"],
            ("assistant",),
            request.lease,
        )

    def test_nonstream_chat_maps_paused_and_completed_results(self) -> None:
        cases = (
            ({"status": next(iter(server.hosted_assistants.CHAT_PAUSED_STATUSES))}, HTTPStatus.PRECONDITION_REQUIRED),
            ({"reply": "done"}, HTTPStatus.OK),
        )
        for result, expected_status in cases:
            with self.subTest(result=result):
                handler = _handler()
                handler._read_body = mock.Mock(return_value={"message": "hello", "files": [], "assistant_ids": []})
                with (
                    mock.patch.object(server.validate, "validate_chat_message", return_value="hello"),
                    mock.patch.object(server.hosted_assistants, "_chat_assistant_ids", return_value=()),
                    mock.patch.object(runtime_state, "_enforce_rate"),
                    mock.patch.object(hosted_chat_api, "_chat", return_value=result),
                ):
                    handler._route_chat_turn(_request(), stream=False)
                handler._send_json.assert_called_once_with(
                    expected_status,
                    result,
                    no_store=expected_status == HTTPStatus.PRECONDITION_REQUIRED,
                )

    def test_integration_pending_route_returns_none_or_the_current_challenge(self) -> None:
        for pending in (None, SimpleNamespace(challenge_id="d" * 32)):
            with self.subTest(pending=pending):
                handler = _handler()
                payload = {"challenge_id": "d" * 32}
                with (
                    mock.patch.object(runtime_state._integration_challenges, "current", return_value=pending),
                    mock.patch.object(
                        server.hosted_chat_segment,
                        "_hosted_integration_challenge_payload",
                        return_value=payload,
                    ),
                ):
                    handler._route_chat_integrations(_request(), submit=False)
                expected = {"team_id": TEAM_ID, "status": "none"} if pending is None else payload
                handler._send_json.assert_called_once_with(HTTPStatus.OK, expected, no_store=True)

    def test_integration_continuation_validates_shape_and_preserves_pause_status(self) -> None:
        handler = _handler()
        handler._read_body = mock.Mock(return_value={"invalid": True})
        with mock.patch.object(runtime_state, "_enforce_rate"), self.assertRaises(runtime_state.ApiError):
            handler._route_chat_integrations(_request(), submit=True)

        paused = {"status": next(iter(server.hosted_assistants.CHAT_PAUSED_STATUSES))}
        handler._read_body = mock.Mock(return_value={"challenge_id": "d" * 32})
        with (
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_chat_api, "_resume_chat_integrations", return_value=paused),
        ):
            handler._route_chat_integrations(_request(), submit=True)
        handler._send_json.assert_called_once_with(HTTPStatus.PRECONDITION_REQUIRED, paused, no_store=True)

    def test_human_continuation_pending_submit_and_stop_are_bound_to_the_team(self) -> None:
        handler = _handler()
        request = _request()
        with mock.patch.object(server.hosted_chat_human, "pending_chat_human", return_value={"status": "none"}):
            handler._route_chat_human(request, submit=False)

        handler._read_body = mock.Mock(return_value={"request_id": "request"})
        with (
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_chat_api, "_resume_chat_human", return_value={"reply": "done"}) as resume,
        ):
            handler._route_chat_human(request, submit=True)
        resume.assert_called_once_with(
            TEAM_ID,
            {"request_id": "request"},
            {"kind": "password"},
            request.lease,
        )

        with (
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(hosted_chat_api, "_stop_chat", return_value={"accepted": False}),
        ):
            handler._route_chat_stop(request)
        self.assertEqual(handler._send_json.call_count, 3)


class HostedHttpAssistantRouteEdgeTests(unittest.TestCase):
    def _install_handler(self, *, source_digest: object = SOURCE_DIGEST):
        handler = _handler()
        handler._read_team_body = mock.Mock(
            return_value={"assistant_id": "example-assistant", "source_digest": source_digest}
        )
        return handler

    def test_install_rejects_a_noncanonical_source_digest(self) -> None:
        handler = self._install_handler(source_digest="latest")
        with self.assertRaises(runtime_state.ApiError):
            handler._route_assistant_install(_request())

    def test_install_rechecks_the_publication_before_runtime_mutation(self) -> None:
        handler = self._install_handler()
        initial = {
            "assistant_id": "example-assistant",
            "source_digest": SOURCE_DIGEST,
            "oci_digest": OCI_DIGEST,
        }
        changed = {**initial, "oci_digest": f"sha256:{'c' * 64}"}
        client = mock.Mock()
        client.resolve.side_effect = [initial, changed]
        handler._publication_dependencies = mock.Mock(return_value=(client, mock.Mock()))
        binding = SimpleNamespace(assistant_id="example-assistant")

        def install(*_args, authorize_start, **_kwargs):
            authorize_start()

        with (
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(server.dynamic_assistants, "binding_from_resolution", return_value=binding),
            mock.patch.object(server.publication, "assistant_spec", return_value=mock.sentinel.spec),
            mock.patch.object(hosted_resources, "_prepare_assistant_image"),
            mock.patch.object(server.publication, "retain_icon"),
            mock.patch.object(server.publication, "discard_icon") as discard,
            mock.patch.object(assistant_lifecycle, "_install_assistant", side_effect=install),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            handler._route_assistant_install(_request())
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        discard.assert_called_once()

    def test_install_maps_each_external_trust_boundary_to_a_closed_public_error(self) -> None:
        exception_cases = (
            (server.developers_client.InstallAuthorizationDeniedError("denied"), HTTPStatus.CONFLICT),
            (server.developers_client.DevelopersClientError("down"), HTTPStatus.SERVICE_UNAVAILABLE),
            (server.artifact_trust.ArtifactTrustError("untrusted"), HTTPStatus.CONFLICT),
            (server.assistant_icons.AssistantIconError("disk"), HTTPStatus.SERVICE_UNAVAILABLE),
        )
        for error, expected_status in exception_cases:
            with self.subTest(error=type(error).__name__):
                handler = self._install_handler()
                client = mock.Mock()
                client.resolve.side_effect = error
                handler._publication_dependencies = mock.Mock(return_value=(client, mock.Mock()))
                with (
                    mock.patch.object(runtime_state, "_enforce_rate"),
                    self.assertRaises(runtime_state.ApiError) as caught,
                ):
                    handler._route_assistant_install(_request())
                self.assertEqual(caught.exception.status, expected_status)

    def test_uninstall_fails_closed_for_unreadable_or_absent_metadata(self) -> None:
        request = _request(assistant_id="example-assistant")
        for binding, expected_status in (
            (server.dynamic_assistants.DynamicAssistantError("unavailable"), HTTPStatus.SERVICE_UNAVAILABLE),
            (None, HTTPStatus.NOT_FOUND),
        ):
            with self.subTest(binding=binding):
                handler = _handler()
                effect = binding if isinstance(binding, Exception) else lambda *_args, _binding=binding: _binding
                with (
                    mock.patch.object(runtime_state._dynamic_assistants, "get", side_effect=effect),
                    self.assertRaises(runtime_state.ApiError) as caught,
                ):
                    handler._route_assistant_uninstall(request)
                self.assertEqual(caught.exception.status, expected_status)

    def test_main_initializes_authorities_before_serving(self) -> None:
        http_server = mock.Mock()
        with (
            mock.patch.object(server.brain_runtime_token_store, "ensure") as ensure,
            mock.patch.object(runtime_state, "_initialize_developers_integration") as initialize,
            mock.patch.object(server, "_BoundedThreadingHTTPServer", return_value=http_server) as constructor,
        ):
            server.main()
        ensure.assert_called_once_with()
        initialize.assert_called_once_with()
        constructor.assert_called_once_with((runtime_state.ALL_INTERFACES, runtime_state.LISTEN_PORT), server.Handler)
        http_server.serve_forever.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
