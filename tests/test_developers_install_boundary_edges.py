from __future__ import annotations

import copy
import json
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from hosted_assistant_fixture import hosted_developers_http as developers_http
from hosted_assistant_fixture import runtime_state

from install.contract import CONTRACT_ROOT

VECTORS = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())
REQUEST = VECTORS["fixtures"]["controller_install_request"]["value"]
CLAIMS = VECTORS["fixtures"]["assistant_install_delegation"]["value"]
RESOLUTION = VECTORS["fixtures"]["resolve_response"]["value"]


class DevelopersInstallBoundaryEdgeTests(unittest.TestCase):
    @staticmethod
    def request(path: str = developers_http.INSTALL_PATH) -> developers_http.RequestIO:
        return developers_http.RequestIO(
            headers=object(),
            path=path,
            capture_body=mock.Mock(),
            read_team_body=mock.Mock(return_value=copy.deepcopy(REQUEST)),
            send_json=mock.Mock(),
        )

    def test_path_dependencies_and_body_rejection_are_closed(self) -> None:
        self.assertTrue(developers_http.is_path(developers_http.TEAMS_PATH))
        self.assertFalse(developers_http.is_path("/other"))
        with (
            mock.patch.multiple(
                runtime_state,
                _developers_delegation=None,
                _developers_client=None,
                _artifact_trust=None,
            ),
            self.assertRaises(runtime_state.ApiError),
        ):
            developers_http._dependencies()

        request = self.request(developers_http.TEAMS_PATH)
        with (
            mock.patch.object(
                developers_http.strict_http,
                "reject_body",
                side_effect=developers_http.strict_http.HttpContractError(
                    HTTPStatus.BAD_REQUEST,
                    "body forbidden",
                    code="unexpected-body",
                ),
            ),
            self.assertRaises(runtime_state.ApiError),
        ):
            developers_http._route_teams(request)

    def test_install_rejects_mismatched_final_authorization_and_discards_icon(self) -> None:
        request = self.request()
        delegation = SimpleNamespace(verify=mock.Mock(return_value=copy.deepcopy(CLAIMS)))
        client = SimpleNamespace(
            resolve=mock.Mock(return_value=copy.deepcopy(RESOLUTION)),
            authorize_install=mock.Mock(return_value={"issued_at": 0, "expires_at": 0}),
        )
        trust = SimpleNamespace(verify=mock.Mock())
        lease = SimpleNamespace(owner=CLAIMS["account_id"])

        def install(*_args, authorize_start, **_kwargs):
            authorize_start()

        with (
            mock.patch.multiple(
                runtime_state,
                _developers_delegation=delegation,
                _developers_client=client,
                _artifact_trust=trust,
                _assistant_icons=object(),
                _dynamic_assistants=object(),
                _enforce_rate=mock.Mock(),
            ),
            mock.patch.object(developers_http.hosted_resources, "_authorize", return_value=lease),
            mock.patch.object(developers_http.hosted_resources, "_prepare_assistant_image"),
            mock.patch.object(developers_http.publication, "assistant_spec", return_value=object()),
            mock.patch.object(developers_http.publication, "retain_icon"),
            mock.patch.object(developers_http.publication, "discard_icon") as discard,
            mock.patch.object(developers_http.assistant_lifecycle, "_install_assistant", side_effect=install),
            self.assertRaises(developers_http.developers_client.InstallAuthorizationDeniedError),
        ):
            developers_http._install(request)
        discard.assert_called_once()

    def test_dispatch_routes_and_rejects_unknown_operation(self) -> None:
        teams = self.request(developers_http.TEAMS_PATH)
        with mock.patch.object(developers_http, "_route_teams") as route:
            developers_http._dispatch(teams, "GET")
        route.assert_called_once_with(teams)

        with self.assertRaises(runtime_state.ApiError):
            developers_http._dispatch(self.request("/unknown"), "PATCH")

    def test_failure_classifier_maps_every_public_family(self) -> None:
        cases = (
            (developers_http.developers_delegation.DevelopersDelegationError("bad"), HTTPStatus.FORBIDDEN),
            (developers_http.developers_client.AssistantNotInstallableError("bad"), HTTPStatus.NOT_FOUND),
            (developers_http.developers_client.InstallAuthorizationDeniedError("bad"), HTTPStatus.CONFLICT),
            (developers_http.artifact_trust.ArtifactTrustError("bad"), HTTPStatus.CONFLICT),
            (developers_http.developers_client.DevelopersClientError("bad"), HTTPStatus.SERVICE_UNAVAILABLE),
            (developers_http.icons.AssistantIconError("bad"), HTTPStatus.SERVICE_UNAVAILABLE),
            (developers_http.docker.errors.DockerException("bad"), HTTPStatus.SERVICE_UNAVAILABLE),
            (OSError("bad"), HTTPStatus.SERVICE_UNAVAILABLE),
            (developers_http.install_contract.ContractValidationError("bad"), HTTPStatus.BAD_REQUEST),
            (developers_http.dynamic_assistants.DynamicAssistantError("bad"), HTTPStatus.BAD_REQUEST),
        )
        for failure, status in cases:
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(developers_http._classify(failure).status, status)
        self.assertIsNone(developers_http._classify(RuntimeError("unknown")))


if __name__ == "__main__":
    unittest.main()
