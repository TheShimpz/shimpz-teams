"""Direct contracts for the hosted Controller authorization chain."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hosted_assistant_fixture import app, hosted_controller, hosted_resources, runtime_state

from hosted import container as container_spec

network_policy = hosted_resources.network_policy

TEAM_ID = "team_1"
CONTAINER_ID = "a" * 64


def _container(*, container_id: str = CONTAINER_ID, owner: str = "account_1") -> SimpleNamespace:
    labels = {"team.id": TEAM_ID, "team.owner": owner}
    return SimpleNamespace(id=container_id, labels=labels, attrs={"Config": {"Labels": labels}})


def _routable_container() -> SimpleNamespace:
    name = container_spec.team_container_name(TEAM_ID)
    labels = {
        "team.runtime": "1",
        "team.id": TEAM_ID,
        "team.owner": "account_1",
    }
    return SimpleNamespace(
        id=CONTAINER_ID,
        name=name,
        status="running",
        labels=labels,
        attrs={"Name": f"/{name}", "Config": {"Labels": labels}},
    )


def _lease(
    *,
    container_id: str = CONTAINER_ID,
    owner: str = "account_1",
    principal: tuple[str, str | None] = ("account", "account_1"),
) -> hosted_resources._AuthorizationLease:
    return hosted_resources._AuthorizationLease(TEAM_ID, container_id, owner, principal)


class HostedAuthorizationTests(unittest.TestCase):
    def test_current_lease_rejects_recreated_or_reowned_team(self) -> None:
        cases = (
            _container(container_id="b" * 64),
            _container(owner="account_2"),
        )

        for current in cases:
            with (
                self.subTest(current=current),
                mock.patch.object(
                    hosted_resources,
                    "_get_container",
                    side_effect=lambda _name, current=current: current,
                ),
                mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
                mock.patch.object(network_policy, "brain_identity_valid", return_value=True),
                self.assertRaises(runtime_state.ApiError) as caught,
            ):
                hosted_resources._require_current_authorization(TEAM_ID, _lease(), require_isolation=False)

            self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)

    def test_account_scope_failure_is_indistinguishable_from_missing_team(self) -> None:
        current = _container()
        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=current),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(network_policy, "brain_identity_valid", return_value=True),
            self.assertRaises(runtime_state.ApiError) as wrong_owner,
        ):
            hosted_resources._require_current_authorization(
                TEAM_ID,
                _lease(principal=("account", "account_2")),
                require_isolation=False,
            )

        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=None),
            self.assertRaises(runtime_state.ApiError) as missing,
        ):
            hosted_resources._authorize(TEAM_ID, ("account", "account_2"))

        self.assertEqual(
            (wrong_owner.exception.status, wrong_owner.exception.message),
            (missing.exception.status, missing.exception.message),
        )

    def test_unknown_principal_kind_is_indistinguishable_from_a_missing_team(self) -> None:
        current = _container()
        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=current),
            mock.patch.object(network_policy, "brain_identity_valid", return_value=True),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_resources._authorize_container(TEAM_ID, ("unknown", "account_1"), current)

        self.assertEqual(
            (caught.exception.status, caught.exception.message),
            (HTTPStatus.NOT_FOUND, f"team {TEAM_ID!r} not found"),
        )

    def test_attributable_supervisor_bypasses_ownership_but_not_container_identity(self) -> None:
        current = _container(owner="account_1")
        with mock.patch.object(network_policy, "brain_identity_valid", return_value=True):
            lease = hosted_resources._authorize_container(TEAM_ID, ("supervisor", "supervisor_account"), current)
        self.assertEqual(lease.owner, "account_1")
        self.assertEqual(lease.principal, ("supervisor", "supervisor_account"))

        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=current),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(network_policy, "brain_identity_valid", return_value=True),
        ):
            self.assertIs(
                hosted_resources._require_current_authorization(TEAM_ID, lease, require_isolation=False),
                current,
            )

        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=current),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(network_policy, "brain_identity_valid", return_value=False),
            self.assertRaises(runtime_state.ApiError) as invalid_identity,
        ):
            hosted_resources._require_current_authorization(TEAM_ID, lease, require_isolation=False)
        self.assertEqual(invalid_identity.exception.status, HTTPStatus.NOT_FOUND)

    def test_pending_cleanup_blocks_normal_use_but_allows_teardown(self) -> None:
        current = _container()
        cleanup = SimpleNamespace(nonce="cleanup")
        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=current),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=cleanup),
            mock.patch.object(network_policy, "brain_identity_valid", return_value=True),
            self.assertRaises(runtime_state.ApiError) as blocked,
        ):
            hosted_resources._require_current_authorization(TEAM_ID, _lease(), require_isolation=False)
        self.assertEqual(blocked.exception.status, HTTPStatus.CONFLICT)

        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=current),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=cleanup),
            mock.patch.object(network_policy, "brain_identity_valid", return_value=True),
        ):
            self.assertIs(
                hosted_resources._require_current_authorization(
                    TEAM_ID,
                    _lease(),
                    require_isolation=False,
                    allow_pending_cleanup=True,
                ),
                current,
            )

    @staticmethod
    def _handler(*headers: tuple[str, str]) -> app.Handler:
        handler = object.__new__(app.Handler)
        handler.headers = Message()
        for name, value in headers:
            handler.headers.add_header(name, value)
        return handler

    @staticmethod
    def _request_handler(path: str, body: bytes, *headers: tuple[str, str]) -> app.Handler:
        handler = HostedAuthorizationTests._handler(*headers)
        handler.path = path
        handler.rfile = BytesIO(body)
        return handler

    def test_human_authority_requires_exact_account_evidence(self) -> None:
        machine = self._handler(("Authorization", "Bearer team-machine-token"))
        account = self._handler(("X-Shimpz-Account", "account-session"))
        duplicate = self._handler(
            ("X-Shimpz-Account", "account-session"),
            ("X-Shimpz-Account", "account-session"),
        )

        self.assertEqual(account._account_session(), "account-session")
        for handler in (machine, duplicate):
            with self.assertRaises(runtime_state.ApiError) as caught:
                handler._account_session()
            self.assertEqual(caught.exception.status, HTTPStatus.FORBIDDEN)

        route = hosted_controller.strict_http.ControllerRouteMatch("team-list", {})
        evidence = hosted_controller.accounts_client.Evaluation("a" * 32, True, "b" * 64, None)
        account._owner_target = mock.Mock(return_value=None)
        with mock.patch.object(
            hosted_controller.accounts_client,
            "evaluate",
            return_value=evidence,
        ) as evaluate:
            result = account._human_authority(
                "account-session",
                "GET",
                route,
                {},
                {},
                {"kind": "none", "length": 0, "sha256": hosted_controller.accounts_client.EMPTY_SHA256},
            )
        self.assertEqual(result, evidence)
        evaluate.assert_called_once()
        self.assertEqual(evaluate.call_args.args[0], "account-session")

    def test_missing_account_session_is_rejected_before_json_body_read(self) -> None:
        body = b'{"team_name":"untrusted"}'
        handler = self._request_handler(
            f"/v1/teams/{TEAM_ID}/create",
            body,
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
        )

        with self.assertRaises(runtime_state.ApiError) as caught:
            handler._dispatch_resolved("POST")

        self.assertEqual(caught.exception.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(handler.rfile.tell(), 0)

    def test_account_denial_and_unavailability_project_with_distinct_audit_outcomes(self) -> None:
        cases = (
            (
                hosted_controller.accounts_client.AuthorityDeniedError("denied"),
                HTTPStatus.FORBIDDEN,
                "denied",
                "credential_rejected",
            ),
            (
                hosted_controller.accounts_client.AuthorityUnavailableError("unavailable"),
                HTTPStatus.SERVICE_UNAVAILABLE,
                "error",
                "credential_present",
            ),
        )
        for error, status, outcome, credential_state in cases:
            handler = self._request_handler(
                "/v1/teams",
                b"",
                ("X-Shimpz-Account", "current-account-session"),
            )
            with (
                self.subTest(status=status),
                mock.patch.object(hosted_controller.accounts_client, "evaluate", side_effect=error),
                mock.patch.object(hosted_controller.audit, "log", return_value="d" * 32) as audit_log,
                self.assertRaises(runtime_state.ApiError) as caught,
            ):
                handler._dispatch_resolved("GET")

            self.assertEqual(caught.exception.status, status)
            self.assertEqual(audit_log.call_args.kwargs["result"], outcome)
            self.assertEqual(audit_log.call_args.kwargs["credential_state"], credential_state)
            self.assertEqual(audit_log.call_args.kwargs["principal_class"], "absent")

    def test_invalid_server_binding_projects_as_unavailable_authority(self) -> None:
        handler = self._request_handler(
            "/v1/teams",
            b"",
            ("X-Shimpz-Account", "current-account-session"),
        )
        with (
            mock.patch.object(
                hosted_controller.accounts_client,
                "binding_digest",
                side_effect=hosted_controller.accounts_client.AuthorityUnavailableError("invalid"),
            ),
            mock.patch.object(hosted_controller.accounts_client, "evaluate") as evaluate,
            mock.patch.object(hosted_controller.audit, "log", return_value="d" * 32) as audit_log,
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            handler._dispatch_resolved("GET")

        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        evaluate.assert_not_called()
        self.assertEqual(audit_log.call_args.kwargs["result"], "error")
        self.assertNotIn("binding_digest", audit_log.call_args.kwargs)

    def test_logs_query_survives_dispatch_and_matches_the_real_account_schema(self) -> None:
        handler = self._request_handler(
            f"/v1/teams/{TEAM_ID}/logs?lines=7",
            b"",
            ("X-Shimpz-Account", "current-account-session"),
        )
        handler._route = mock.Mock()
        evidence = hosted_controller.accounts_client.Evaluation("a" * 32, False, "c" * 64, None)

        with mock.patch.object(
            hosted_controller.accounts_client,
            "evaluate",
            return_value=evidence,
        ) as evaluate:
            handler._dispatch_resolved("GET")

        binding = evaluate.call_args.args[1]
        self.assertEqual(binding["query"], {"lines": "7"})
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "protocol"
            / "account"
            / "authority"
            / "v1"
            / "evaluation-request.schema.json"
        )
        validator = Draft202012Validator(json.loads(schema_path.read_bytes()))
        self.assertFalse(tuple(validator.iter_errors({"version": 1, "session_token": "x", "binding": binding})))

    def test_operation_audit_reuses_the_attributable_authority_trace(self) -> None:
        handler = self._handler()
        handler._audit_trace_id = "d" * 32
        handler._audit_account_id = "a" * 32
        handler._audit_supervisor = True
        handler._audit_credential_state = "credential_present"
        with mock.patch.object(hosted_controller.audit, "log", return_value="d" * 32) as audit_log:
            trace = handler._audit_security("destroy", TEAM_ID, result="ok")

        self.assertEqual(trace, "d" * 32)
        self.assertEqual(audit_log.call_args.kwargs["trace_id"], "d" * 32)
        self.assertEqual(audit_log.call_args.kwargs["principal_id"], "a" * 32)
        self.assertTrue(audit_log.call_args.kwargs["supervisor"])

    def test_dispatch_binds_exact_json_and_supervisor_owner_assignment(self) -> None:
        owner = "b" * 32
        body = (
            b'{ "team_name": "Marketing", '
            b'"owner_account_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }'
        )
        handler = self._request_handler(
            f"/v1/teams/{TEAM_ID}/create",
            body,
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
            ("X-Shimpz-Account", "current-account-session"),
            ("Authorization", "Bearer team-machine-token"),
        )
        handler._route = mock.Mock()
        evidence = hosted_controller.accounts_client.Evaluation("a" * 32, True, "c" * 64, owner)

        with mock.patch.object(
            hosted_controller.accounts_client,
            "evaluate",
            return_value=evidence,
        ) as evaluate:
            handler._dispatch_resolved("POST")

        binding = evaluate.call_args.args[1]
        self.assertEqual(evaluate.call_args.args[0], "current-account-session")
        self.assertEqual(
            binding,
            {
                "method": "POST",
                "operation": "team-create",
                "params": {"team_id": TEAM_ID},
                "query": {},
                "body": {
                    "kind": "json",
                    "length": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                },
                "owner_account_id": owner,
            },
        )
        handler._route.assert_called_once()
        self.assertIs(handler._route.call_args.args[3], evidence)

    def test_file_content_is_unread_until_account_authorizes_exact_metadata(self) -> None:
        body = b"private Team file"
        handler = self._request_handler(
            f"/v1/teams/{TEAM_ID}/files",
            body,
            ("Content-Length", str(len(body))),
            ("Content-Type", "text/plain"),
            ("X-Shimpz-Filename", "brief.txt"),
            ("X-Shimpz-Account", "current-account-session"),
        )
        handler._route = mock.Mock()
        evidence = hosted_controller.accounts_client.Evaluation("a" * 32, False, "c" * 64, None)

        with mock.patch.object(
            hosted_controller.accounts_client,
            "evaluate",
            return_value=evidence,
        ) as evaluate:
            handler._dispatch_resolved("POST")

        self.assertEqual(handler.rfile.tell(), 0)
        self.assertEqual(
            evaluate.call_args.args[1]["body"],
            {
                "kind": "file",
                "length": len(body),
                "filename": "brief.txt",
                "media_type": "text/plain",
            },
        )
        self.assertEqual(handler._read_file_body(), ("brief.txt", body, "text/plain"))

    def test_machine_bearer_is_limited_to_the_one_use_oauth_callback(self) -> None:
        body = b'{"state":"opaque","code":"claim","session_binding":"browser"}'
        accepted = self._request_handler(
            "/v1/oauth/cloudflare/callback",
            body,
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
            ("Authorization", "Bearer team-machine-token"),
        )
        accepted._route_assistant_integration_complete = mock.Mock()
        with mock.patch.object(hosted_controller.accounts_client, "evaluate") as evaluate:
            accepted._dispatch_resolved("POST")
        accepted._route_assistant_integration_complete.assert_called_once_with()
        evaluate.assert_not_called()

        duplicate = self._request_handler(
            "/v1/oauth/cloudflare/callback",
            body,
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
            ("Authorization", "Bearer team-machine-token"),
            ("Authorization", "Bearer team-machine-token"),
        )
        with self.assertRaises(runtime_state.ApiError) as denied:
            duplicate._dispatch_resolved("POST")
        self.assertEqual(denied.exception.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(duplicate.rfile.tell(), 0)

    def test_sensitive_route_drives_the_real_current_authorization_chain(self) -> None:
        container = _routable_container()
        handler = self._handler()
        handler.path = f"/v1/teams/{TEAM_ID}/assistants"
        sent: list[tuple[HTTPStatus, dict]] = []
        handler._send_json = lambda status, payload, **_kwargs: sent.append((status, payload))

        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=container),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(
                hosted_controller.hosted_apps,
                "_team_assistant_containers",
                return_value=[],
            ),
            mock.patch.object(
                hosted_resources,
                "_require_current_authorization",
                wraps=hosted_resources._require_current_authorization,
            ) as require_current,
        ):
            route = hosted_controller.strict_http.resolve_controller_route(
                hosted_controller.strict_http.HOSTED_CONTROLLER,
                "GET",
                ("v1", "teams", TEAM_ID, "assistants"),
            )
            self.assertIsNotNone(route)
            handler._route(
                route,
                {"team_id": TEAM_ID},
                {},
                hosted_controller.accounts_client.Evaluation("account_1", False, "b" * 64, None),
            )

        self.assertEqual(sent, [(HTTPStatus.OK, {"assistants": []})])
        require_current.assert_called_once()
        self.assertEqual(require_current.call_args.args[0], TEAM_ID)
        self.assertEqual(require_current.call_args.args[1].container_id, CONTAINER_ID)
        self.assertEqual(require_current.call_args.kwargs, {"require_isolation": False})


if __name__ == "__main__":
    unittest.main()
