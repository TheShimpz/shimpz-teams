"""Hosted Controller boundary for delegated Developers installation."""

from __future__ import annotations

import copy
import json
import time
import types
import unittest
from http import HTTPStatus
from unittest import mock

from hosted_assistant_fixture import (
    hosted_apps,
    hosted_controller,
    hosted_lifecycle,
    hosted_resources,
    runtime_state,
)
from hosted_assistant_fixture import (
    hosted_developers_http as developers_http,
)

CONTRACT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1] / "protocol" / "install" / "v1"
VECTORS = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())
REQUEST = VECTORS["fixtures"]["controller_install_request"]["value"]
CLAIMS = VECTORS["fixtures"]["assistant_install_delegation"]["value"]
RESOLUTION = VECTORS["fixtures"]["resolve_response"]["value"]


class _Delegation:
    def verify(self, _headers, *, action: str, request=None):
        expected_action = "teams:list" if request is None else "assistant:install"
        if action != expected_action:
            raise AssertionError("wrong delegation action")
        return copy.deepcopy(CLAIMS)


class _Client:
    def __init__(self) -> None:
        self.authorization = None

    def resolve(self, source_digest: str):
        if source_digest != RESOLUTION["source_digest"]:
            raise AssertionError("wrong source digest")
        return copy.deepcopy(RESOLUTION)

    def authorize_install(self, request):
        self.authorization = request
        issued_at = int(time.time())
        return {
            "version": 1,
            "authorization_id": "authorization_1",
            **{key: value for key, value in request.items() if key != "version"},
            "issued_at": issued_at,
            "expires_at": issued_at + 60,
        }


class DevelopersInstallBoundaryTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(hosted_controller.Handler)
        handler.headers = object()
        handler._read_team_body = mock.Mock(return_value=copy.deepcopy(REQUEST))
        handler._send_json = mock.Mock()
        return handler

    @staticmethod
    def _request(handler, path: str = developers_http.INSTALL_PATH):
        return developers_http.RequestIO(
            handler.headers,
            path,
            handler._capture_body,
            handler._read_team_body,
            handler._send_json,
        )

    def test_install_binds_owner_trust_final_authorization_and_response(self) -> None:
        handler = self._handler()
        client = _Client()
        events: list[str] = []
        original_resolve = client.resolve
        original_authorize_install = client.authorize_install
        client.resolve = mock.Mock(
            side_effect=lambda source_digest: (
                events.append("resolve"),
                original_resolve(source_digest),
            )[1]
        )
        client.authorize_install = mock.Mock(
            side_effect=lambda request: (
                events.append("authorize-start"),
                original_authorize_install(request),
            )[1]
        )
        trust = types.SimpleNamespace(verify=mock.Mock(side_effect=lambda _resolution: events.append("trust")))
        delegation = _Delegation()
        lease = types.SimpleNamespace(owner=CLAIMS["account_id"])
        materialized_spec = types.SimpleNamespace(
            image=RESOLUTION["image_reference"],
        )

        def install(team_id, binding, owner, supplied_lease, *, authorize_start):
            events.append("install")
            self.assertEqual(team_id, REQUEST["team_id"])
            self.assertEqual(owner, CLAIMS["account_id"])
            self.assertIs(supplied_lease, lease)
            authorize_start()
            return {
                "source_digest": binding.resolution["source_digest"],
                "oci_digest": binding.resolution["oci_digest"],
                "binding_digest": binding.binding_digest,
            }

        def prepare_image(spec):
            events.append("prepare-image")
            self.assertEqual(spec.image, RESOLUTION["image_reference"])

        with (
            mock.patch.multiple(
                runtime_state,
                _developers_delegation=delegation,
                _developers_client=client,
                _artifact_trust=trust,
                _enforce_rate=lambda *_args: None,
            ),
            mock.patch.object(hosted_resources, "_authorize", return_value=lease) as authorize,
            mock.patch.object(
                hosted_controller.publication,
                "assistant_spec",
                return_value=materialized_spec,
            ) as assistant_spec,
            mock.patch.object(hosted_resources, "_prepare_assistant_image", side_effect=prepare_image),
            mock.patch.object(hosted_apps, "_install_assistant", side_effect=install),
        ):
            developers_http._install(self._request(handler))

        authorize.assert_called_once_with(
            REQUEST["team_id"],
            ("account", CLAIMS["account_id"]),
        )
        trust.verify.assert_called_once_with(RESOLUTION)
        assistant_spec.assert_called_once()
        self.assertEqual(events, ["resolve", "trust", "prepare-image", "install", "authorize-start"])
        self.assertEqual(client.authorization["delegation_jti"], CLAIMS["jti"])
        status, response = handler._send_json.call_args.args
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(response["status"], "installed")
        self.assertEqual(response["assistant_id"], RESOLUTION["assistant_id"])
        self.assertEqual(response["source_digest"], REQUEST["source_digest"])

    def test_install_dispatch_captures_the_document_once_before_routing(self) -> None:
        handler = self._handler()
        events: list[object] = []
        handler._capture_body = mock.Mock(side_effect=lambda operation: events.append(operation))
        with mock.patch.object(developers_http, "_install", side_effect=lambda _request: events.append("route")):
            developers_http.dispatch(self._request(handler), "POST")

        self.assertEqual(events, ["assistant-install", "route"])

    def test_team_listing_exposes_only_contract_fields(self) -> None:
        handler = self._handler()
        handler.headers = types.SimpleNamespace(get_all=lambda *_args, **_kwargs: [])
        listing = {
            "teams": [
                {
                    "team_id": "team_1",
                    "team_name": "First Team",
                    "owner": CLAIMS["account_id"],
                    "container": "secret-internal-name",
                }
            ]
        }
        with (
            mock.patch.multiple(
                runtime_state,
                _developers_delegation=_Delegation(),
                _developers_client=_Client(),
                _artifact_trust=types.SimpleNamespace(),
            ),
            mock.patch.object(hosted_controller.strict_http, "reject_body"),
            mock.patch.object(hosted_lifecycle, "_list", return_value=listing) as list_teams,
            mock.patch.object(developers_http.audit, "log") as audit_log,
        ):
            developers_http._route_teams(self._request(handler, developers_http.TEAMS_PATH))

        list_teams.assert_called_once_with(owner=CLAIMS["account_id"])
        self.assertEqual(
            handler._send_json.call_args.args,
            (HTTPStatus.OK, {"version": 1, "teams": [{"id": "team_1", "name": "First Team"}]}),
        )
        self.assertEqual(audit_log.call_args.kwargs["principal_id"], "developers")
        self.assertEqual(audit_log.call_args.kwargs["subject_account_id"], CLAIMS["account_id"])

    def test_dispatch_redacts_unexpected_failures_and_audits_explicit_principal_state(self) -> None:
        for account_id, expected_class in ((None, "absent"), (CLAIMS["account_id"], "machine")):
            handler = self._handler()
            request = self._request(handler)
            request.account_id = account_id
            with (
                self.subTest(account_id=account_id),
                mock.patch.object(developers_http, "_dispatch", side_effect=RuntimeError("protected detail")),
                mock.patch.object(developers_http.audit, "log") as audit_log,
            ):
                developers_http.dispatch(request, "POST")

            status, payload = handler._send_json.call_args.args
            self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertNotIn("protected detail", json.dumps(payload))
            self.assertEqual(audit_log.call_args.kwargs["reason"], "RuntimeError")
            self.assertEqual(audit_log.call_args.kwargs["principal_class"], expected_class)

    def test_api_failures_classify_server_errors_as_errors_and_client_failures_as_denials(self) -> None:
        server = developers_http._runtime_failure(
            runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "unavailable")
        )
        client = developers_http._runtime_failure(runtime_state.ApiError(HTTPStatus.CONFLICT, "denied"))

        self.assertEqual(server.result, "error")
        self.assertEqual(client.result, "denied")

    def test_install_authorization_allows_only_bounded_future_clock_skew(self) -> None:
        expected = {
            "account_id": CLAIMS["account_id"],
            "team_id": REQUEST["team_id"],
        }
        receipt = {
            **expected,
            "issued_at": 1_005,
            "expires_at": 1_060,
        }

        self.assertTrue(developers_http._install_authorization_matches(receipt, expected, 1_000))
        receipt["issued_at"] = 1_006
        self.assertFalse(developers_http._install_authorization_matches(receipt, expected, 1_000))
        receipt["issued_at"] = 1_000
        receipt["expires_at"] = 999
        self.assertFalse(developers_http._install_authorization_matches(receipt, expected, 1_000))
        receipt["expires_at"] = 1_060
        receipt["team_id"] = "other_team"
        self.assertFalse(developers_http._install_authorization_matches(receipt, expected, 1_000))


if __name__ == "__main__":
    unittest.main()
