from __future__ import annotations

import sys
import time
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from assistant.spec import IntegrationSpec, PowerSpec
from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from local.install.runtime import AssistantSpec


@dataclass(frozen=True)
class _Active:
    spec: AssistantSpec


@dataclass(frozen=True)
class _Integration:
    id: str
    username: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class _Metadata:
    id: str
    provider: str
    scopes: tuple[str, ...]
    status: str
    integration: _Integration | None
    expires_at: int | None
    generation: int
    access_token: str = "-".join(("must", "never", "be", "public"))
    refresh_token: str = "-".join(("must", "never", "be", "public"))


class _Store:
    def __init__(
        self,
        rows: dict[tuple[str, str], _Metadata],
        tokens: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.rows = rows
        self.tokens = tokens or {}
        self.resolved: list[tuple[object, ...]] = []

    def metadata(self, team_id: object, assistant_id: object, declarations: object) -> tuple[_Metadata, ...]:
        assert isinstance(assistant_id, str)
        assert isinstance(declarations, dict)
        return tuple(self.rows[(assistant_id, integration_id)] for integration_id in declarations)

    def resolve(
        self,
        team_id: object,
        assistant_id: object,
        integration_id: object,
        provider: object,
        scopes: object,
        refresh_callback: object,
    ) -> str:
        assert isinstance(assistant_id, str)
        assert isinstance(integration_id, str)
        assert callable(refresh_callback)
        self.resolved.append((team_id, assistant_id, integration_id, provider, scopes, refresh_callback))
        return self.tokens[(assistant_id, integration_id)]


def _spec() -> AssistantSpec:
    read_scopes = ("dns.read", "zone.read")
    write_scopes = ("dns.read", "offline_access", "zone.read")
    return AssistantSpec(
        assistant_id="cloudflare-assistant",
        name="Cloudflare Assistant",
        summary="test",
        image="example.invalid/x@sha256:" + ("a" * 64),
        powers={
            "read-profile": PowerSpec(
                "Read one external profile.",
                {},
                {},
                ("cloudflare-read",),
            ),
            "publish-post": PowerSpec(
                "Publish one approved external update.",
                {},
                {},
                ("cloudflare-write",),
            ),
        },
        allowed_hosts=("api.cloudflare.com",),
        required_image_labels=(
            ("org.shimpz.assistant.id", "cloudflare-assistant"),
            ("org.shimpz.source.digest", "sha256:" + ("a" * 64)),
        ),
        integrations={
            "cloudflare-read": IntegrationSpec("cloudflare", read_scopes),
            "cloudflare-write": IntegrationSpec("cloudflare", write_scopes),
        },
    )


def _request(power: str, interrupt_id: str) -> brain_runtime_client.PowerRequest:
    return brain_runtime_client.PowerRequest(interrupt_id, "cloudflare-assistant", power, {})


def _cloudflare_spec() -> AssistantSpec:
    return AssistantSpec(
        assistant_id="shimpz-cloudflare",
        name="Shimpz Cloudflare",
        summary="test",
        image="example.invalid/cloudflare@sha256:" + ("b" * 64),
        powers={
            "list-zones": PowerSpec(
                "List a bounded page of Cloudflare zones and domains.",
                {},
                {},
                ("cloudflare",),
            )
        },
        allowed_hosts=("api.cloudflare.com",),
        required_image_labels=(
            ("org.shimpz.assistant.id", "shimpz-cloudflare"),
            ("org.shimpz.source.digest", "sha256:" + ("b" * 64)),
        ),
        integrations={
            "cloudflare": IntegrationSpec(
                "cloudflare",
                ("dns.read", "dns.write", "offline_access", "zone.read"),
            ),
        },
    )


class AssistantIntegrationFlowTests(unittest.TestCase):
    def test_batch_collects_every_unusable_integration_before_any_power(self) -> None:
        expiry = int(time.time()) + 3600
        spec = _spec()
        store = _Store(
            {
                ("cloudflare-assistant", "cloudflare-read"): _Metadata(
                    "cloudflare-read",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-read"].scopes)),
                    "connected",
                    _Integration("123", "reader", "Reader"),
                    expiry,
                    1,
                ),
                ("cloudflare-assistant", "cloudflare-write"): _Metadata(
                    "cloudflare-write",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-write"].scopes)),
                    "reauthorization-required",
                    _Integration("123", "reader", "Reader"),
                    expiry,
                    2,
                ),
            }
        )

        requirements = integration_flow.requirements_for_batch(
            "team_1",
            {"cloudflare-assistant": _Active(spec)},
            (_request("read-profile", "one"), _request("publish-post", "two")),
            store,
        )

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].power_ids, ("publish-post",))
        self.assertEqual(
            requirements[0].integrations,
            (("cloudflare-write", "cloudflare", ("dns.read", "offline_access", "zone.read")),),
        )

    def test_batch_treats_refresh_required_integration_as_machine_recoverable(self) -> None:
        spec = _spec()
        store = _Store(
            {
                ("cloudflare-assistant", "cloudflare-write"): _Metadata(
                    "cloudflare-write",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-write"].scopes)),
                    "refresh-required",
                    _Integration("123", "reader", "Reader"),
                    int(time.time()) - 1,
                    2,
                ),
            }
        )

        requirements = integration_flow.requirements_for_batch(
            "team_1",
            {"cloudflare-assistant": _Active(spec)},
            (_request("publish-post", "one"),),
            store,
        )

        self.assertEqual(requirements, ())

    def test_challenge_is_exact_bounded_public_metadata(self) -> None:
        spec = _spec()
        requirement = integration_challenges.IntegrationRequirement(
            "cloudflare-assistant",
            "Cloudflare Assistant",
            ("publish-post",),
            (("cloudflare-write", "cloudflare", ("dns.read", "offline_access", "zone.read")),),
        )
        challenge = integration_challenges.PendingIntegrationChallenge(
            "a" * 32,
            "team_1",
            time.monotonic() + 600,
            (requirement,),
            {"input": "must-never-be-public"},
        )

        payload = integration_flow.challenge_payload(
            challenge,
            {"cloudflare-assistant": _Active(spec)},
        )

        self.assertEqual(
            set(payload),
            {"team_id", "status", "turn_id", "challenge_id", "expires_in", "requirements"},
        )
        self.assertEqual(payload["status"], "integrations-required")
        self.assertIn(payload["expires_in"], {599, 600})
        self.assertEqual(
            payload["requirements"],
            [
                {
                    "assistant_id": "cloudflare-assistant",
                    "assistant_name": "Cloudflare Assistant",
                    "integration_id": "cloudflare-write",
                    "provider": "cloudflare",
                    "name": "Cloudflare",
                    "summary": (
                        "Connect your Cloudflare integration so this Assistant can use only "
                        "its reviewed Cloudflare permissions."
                    ),
                    "scopes": ["dns.read", "offline_access", "zone.read"],
                    "powers": [
                        {
                            "id": "publish-post",
                            "name": "Publish Post",
                            "summary": "Publish one approved external update.",
                        }
                    ],
                }
            ],
        )
        self.assertNotIn("must-never-be-public", repr(payload))
        self.assertNotIn("access_token", repr(payload))

    def test_cloudflare_challenge_projects_reviewed_oauth_metadata(self) -> None:
        spec = _cloudflare_spec()
        requirement = integration_challenges.IntegrationRequirement(
            "shimpz-cloudflare",
            "Shimpz Cloudflare",
            ("list-zones",),
            (("cloudflare", "cloudflare", ("dns.read", "dns.write", "offline_access", "zone.read")),),
        )
        challenge = integration_challenges.PendingIntegrationChallenge(
            "b" * 32,
            "team_1",
            time.monotonic() + 300,
            (requirement,),
            {"input": "must-never-be-public"},
        )

        payload = integration_flow.challenge_payload(
            challenge,
            {"shimpz-cloudflare": _Active(spec)},
        )

        self.assertEqual(
            payload["requirements"],
            [
                {
                    "assistant_id": "shimpz-cloudflare",
                    "assistant_name": "Shimpz Cloudflare",
                    "integration_id": "cloudflare",
                    "provider": "cloudflare",
                    "name": "Cloudflare",
                    "summary": (
                        "Connect your Cloudflare integration so this Assistant can use only "
                        "its reviewed Cloudflare permissions."
                    ),
                    "scopes": ["dns.read", "dns.write", "offline_access", "zone.read"],
                    "powers": [
                        {
                            "id": "list-zones",
                            "name": "List Zones",
                            "summary": "List a bounded page of Cloudflare zones and domains.",
                        }
                    ],
                }
            ],
        )
        self.assertNotIn("must-never-be-public", repr(payload))

    def test_inventory_flattens_status_without_token_or_generation_fields(self) -> None:
        spec = _spec()
        expiry = 1_800_000_000
        store = _Store(
            {
                ("cloudflare-assistant", "cloudflare-read"): _Metadata(
                    "cloudflare-read",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-read"].scopes)),
                    "missing",
                    None,
                    None,
                    0,
                ),
                ("cloudflare-assistant", "cloudflare-write"): _Metadata(
                    "cloudflare-write",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-write"].scopes)),
                    "refresh-required",
                    _Integration("123", "juliano", "Juliano"),
                    expiry,
                    4,
                ),
            }
        )

        payload = integration_flow.inventory_payload("team_1", [spec], store)

        self.assertEqual(set(payload), {"integrations"})
        self.assertEqual(payload["integrations"][0]["status"], "missing")
        self.assertEqual(payload["integrations"][1]["status"], "expired")
        self.assertEqual(
            payload["integrations"][1]["integration"],
            {"id": "123", "name": "Juliano", "username": "juliano"},
        )
        self.assertEqual(
            payload["integrations"][1]["expires_at"],
            datetime.fromtimestamp(expiry, UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        encoded = repr(payload)
        for forbidden in ("access_token", "refresh_token", "must-never-be-public", "generation"):
            self.assertNotIn(forbidden, encoded)

    def test_private_resolution_returns_only_the_selected_power_integration(self) -> None:
        spec = _spec()
        token = "-".join(("private", "access", "token", "123456"))
        store = _Store({}, {("cloudflare-assistant", "cloudflare-write"): token})
        refresh_calls: list[tuple[str, tuple[str, ...], str, str | None]] = []

        integrations = integration_flow.resolve_power_integrations(
            "team_1",
            spec,
            "publish-post",
            store,
            lambda provider, scopes, refresh, lease: refresh_calls.append((provider, scopes, refresh, lease)),
        )

        self.assertEqual(
            integrations,
            {"cloudflare-write": {"type": "oauth2-bearer", "access_token": token}},
        )
        self.assertEqual(len(store.resolved), 1)
        callback = store.resolved[0][-1]
        callback("private-refresh-token-123", "private-broker-lease-123")
        self.assertEqual(
            refresh_calls,
            [
                (
                    "cloudflare",
                    ("dns.read", "offline_access", "zone.read"),
                    "private-refresh-token-123",
                    "private-broker-lease-123",
                )
            ],
        )

    def test_flow_fails_closed_on_drift_sensitive_public_fields_and_invalid_tokens(self) -> None:
        spec = _spec()
        drifted = _Store(
            {
                ("cloudflare-assistant", "cloudflare-read"): _Metadata(
                    "cloudflare-read",
                    "cloudflare",
                    ("dns.read",),
                    "connected",
                    None,
                    int(time.time()) + 60,
                    1,
                )
            }
        )
        with self.assertRaises(integration_flow.IntegrationFlowError):
            integration_flow.requirements_for_batch(
                "team_1",
                {"cloudflare-assistant": _Active(spec)},
                (_request("read-profile", "one"),),
                drifted,
            )
        with self.assertRaises(integration_flow.IntegrationFlowError):
            integration_flow._assert_public_payload({"access_token": "private"})

        invalid_token_store = _Store({}, {("cloudflare-assistant", "cloudflare-read"): "short"})
        with self.assertRaises(integration_flow.IntegrationFlowError):
            integration_flow.resolve_power_integrations(
                "team_1",
                spec,
                "read-profile",
                invalid_token_store,
                lambda _provider, _scopes, _refresh: object(),
            )

    def test_identifier_public_text_assistant_and_declaration_edges(self) -> None:
        for function, arguments in (
            (integration_flow._team_id, ("../team",)),
            (integration_flow._component_id, ("Bad", "component")),
        ):
            with self.subTest(function=function.__name__), self.assertRaises(integration_flow.IntegrationFlowError):
                function(*arguments)
        self.assertIsNone(integration_flow._public_text(None, "optional", optional=True))
        for value in (None, "", " padded ", "\ud800", "x" * (integration_flow.MAX_PUBLIC_TEXT_BYTES + 1)):
            with self.subTest(value=repr(value)), self.assertRaises(integration_flow.IntegrationFlowError):
                integration_flow._public_text(value, "text")

        for spec in (
            object(),
            SimpleNamespace(assistant_id="assistant", name="Assistant", powers=[], integrations={}),
            SimpleNamespace(
                assistant_id="assistant",
                name="Assistant",
                powers={},
                integrations={str(index): object() for index in range(integration_flow.MAX_INTEGRATIONS_PER_POWER + 1)},
            ),
        ):
            with self.subTest(spec=spec), self.assertRaises(integration_flow.IntegrationFlowError):
                integration_flow._assistant(spec)

        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "declaration is invalid"):
            integration_flow._intent("integration", object())
        with (
            mock.patch.object(
                integration_flow.integration_providers,
                "resolve",
                return_value=SimpleNamespace(id="foreign"),
            ),
            self.assertRaisesRegex(integration_flow.IntegrationFlowError, "public metadata"),
        ):
            integration_flow._provider_metadata("foreign")

    def test_power_and_metadata_inventory_shapes_fail_closed(self) -> None:
        spec = _spec()
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "unavailable"):
            integration_flow._power(spec, "missing")
        malformed = SimpleNamespace(summary="Summary")
        for power in (
            malformed,
            SimpleNamespace(summary="Summary", integrations=[]),
            SimpleNamespace(summary="Summary", integrations=("one", "one")),
        ):
            candidate = SimpleNamespace(powers={"power": power})
            with self.subTest(power=power), self.assertRaises(integration_flow.IntegrationFlowError):
                integration_flow._power(candidate, "power")

        declarations = {"cloudflare-read": spec.integrations["cloudflare-read"]}
        flow_error = integration_flow.IntegrationFlowError("direct")
        for side_effect, message in ((flow_error, "direct"), (OSError("offline"), "inventory is unavailable")):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(integration_flow.IntegrationFlowError, message),
            ):
                integration_flow._metadata_for(
                    "team_1",
                    spec,
                    declarations,
                    SimpleNamespace(metadata=mock.Mock(side_effect=side_effect)),
                )

        for rows in (
            [],
            (object(),),
            (
                _Metadata(
                    "cloudflare-read",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-read"].scopes)),
                    "invalid",
                    None,
                    None,
                    0,
                ),
            ),
            (
                _Metadata(
                    "cloudflare-read",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-read"].scopes)),
                    "missing",
                    _Integration("identity"),
                    None,
                    0,
                ),
            ),
            (
                _Metadata(
                    "cloudflare-read",
                    "cloudflare",
                    tuple(sorted(spec.integrations["cloudflare-read"].scopes)),
                    "connected",
                    None,
                    None,
                    1,
                ),
            ),
        ):
            with self.subTest(rows=rows), self.assertRaises(integration_flow.IntegrationFlowError):
                integration_flow._metadata_for(
                    "team_1",
                    spec,
                    declarations,
                    SimpleNamespace(metadata=lambda *_args, rows=rows: rows),
                )

    def test_batch_request_binding_declaration_and_requirement_limits_fail_closed(self) -> None:
        spec = _spec()
        valid_store = _Store({})
        for requests, bindings in (
            ("requests", {}),
            ((object(),), {}),
            ((_request("read-profile", "one"),), {}),
            (
                (_request("read-profile", "one"),),
                {"cloudflare-assistant": _Active(replace(spec, assistant_id="other"))},
            ),
        ):
            with self.subTest(requests=requests), self.assertRaises(integration_flow.IntegrationFlowError):
                integration_flow.requirements_for_batch("team_1", bindings, requests, valid_store)

        undeclared = _spec()
        undeclared.powers["read-profile"] = PowerSpec("Read.", {}, {}, ("missing",))
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "undeclared"):
            integration_flow.requirements_for_batch(
                "team_1",
                {"cloudflare-assistant": _Active(undeclared)},
                (_request("read-profile", "one"),),
                valid_store,
            )

        missing = _Metadata(
            "cloudflare-read",
            "cloudflare",
            tuple(sorted(spec.integrations["cloudflare-read"].scopes)),
            "missing",
            None,
            None,
            0,
        )
        store = _Store({("cloudflare-assistant", "cloudflare-read"): missing})
        with (
            mock.patch.object(integration_flow, "MAX_INTEGRATION_REQUIREMENTS", 0),
            self.assertRaisesRegex(integration_flow.IntegrationFlowError, "too many"),
        ):
            integration_flow.requirements_for_batch(
                "team_1",
                {"cloudflare-assistant": _Active(spec)},
                (_request("read-profile", "one"),),
                store,
            )

    def test_challenge_expiry_public_projection_and_drift_edges_fail_closed(self) -> None:
        requirement = integration_challenges.IntegrationRequirement(
            "cloudflare-assistant",
            "Cloudflare Assistant",
            ("publish-post",),
            (("cloudflare-write", "cloudflare", ("dns.read", "offline_access", "zone.read")),),
        )
        for challenge in (
            SimpleNamespace(expires_at="invalid"),
            integration_challenges.PendingIntegrationChallenge("a" * 32, "team_1", 0, (requirement,), object()),
        ):
            with self.subTest(challenge=challenge), self.assertRaises(integration_flow.IntegrationFlowError):
                integration_flow._expires_in(challenge)

        spec = _spec()
        valid = integration_challenges.PendingIntegrationChallenge(
            "a" * 32,
            "team_1",
            time.monotonic() + 60,
            (requirement,),
            object(),
        )
        malformed_requirements = (
            (),
            (replace(requirement, integrations=()),),
        )
        for requirements in malformed_requirements:
            challenge = integration_challenges.PendingIntegrationChallenge(
                valid.id,
                valid.team_id,
                valid.expires_at,
                requirements,
                object(),
            )
            with self.subTest(requirements=requirements), self.assertRaises(integration_flow.IntegrationFlowError):
                integration_flow.challenge_payload(challenge, {"cloudflare-assistant": _Active(spec)})

        drift_cases = (
            ({}, "unavailable"),
            ({"cloudflare-assistant": _Active(replace(spec, name="Changed"))}, "changed"),
        )
        for bindings, message in drift_cases:
            with self.subTest(message=message), self.assertRaisesRegex(integration_flow.IntegrationFlowError, message):
                integration_flow.challenge_payload(valid, bindings)

        changed_declaration = _spec()
        changed_declaration.integrations.pop("cloudflare-write")
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "declaration changed"):
            integration_flow.challenge_payload(valid, {"cloudflare-assistant": _Active(changed_declaration)})

        changed_power = _spec()
        changed_power.powers["publish-post"] = PowerSpec("Publish.", {}, {}, ())
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "Power changed"):
            integration_flow.challenge_payload(valid, {"cloudflare-assistant": _Active(changed_power)})

        duplicate_power = replace(requirement, power_ids=("publish-post", "publish-post"))
        duplicate = integration_challenges.PendingIntegrationChallenge(
            valid.id,
            valid.team_id,
            valid.expires_at,
            (duplicate_power,),
            object(),
        )
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "Power list"):
            integration_flow.challenge_payload(duplicate, {"cloudflare-assistant": _Active(spec)})

    def test_inventory_and_private_resolution_edges_fail_closed(self) -> None:
        spec = _spec()
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "metadata is invalid"):
            integration_flow._integration_payload(object())
        for expiry in (True, 0, 2**53 - 1):
            with (
                self.subTest(expiry=expiry),
                self.assertRaisesRegex(
                    integration_flow.IntegrationFlowError,
                    "expiry is invalid",
                ),
            ):
                integration_flow._expiry_payload(expiry)

        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "too large"):
            integration_flow.inventory_payload("team_1", "assistants", _Store({}))
        spec_without_integrations = replace(spec, integrations={})
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "inventory is invalid"):
            integration_flow.inventory_payload(
                "team_1",
                [spec_without_integrations, spec_without_integrations],
                _Store({}),
            )

        with (
            mock.patch.object(integration_flow, "MAX_INVENTORY_INTEGRATIONS", 0),
            self.assertRaisesRegex(integration_flow.IntegrationFlowError, "too large"),
        ):
            missing_rows = {
                ("cloudflare-assistant", identifier): _Metadata(
                    identifier,
                    "cloudflare",
                    tuple(sorted(declaration.scopes)),
                    "missing",
                    None,
                    None,
                    0,
                )
                for identifier, declaration in spec.integrations.items()
            }
            integration_flow.inventory_payload("team_1", [spec], _Store(missing_rows))

        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "callback is invalid"):
            integration_flow.resolve_power_integrations("team_1", spec, "read-profile", _Store({}), None)

        undeclared = _spec()
        undeclared.powers["read-profile"] = PowerSpec("Read.", {}, {}, ("missing",))
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "undeclared"):
            integration_flow.resolve_power_integrations(
                "team_1",
                undeclared,
                "read-profile",
                _Store({}),
                lambda *_: None,
            )

        for token in (None, "é" * 20):
            store = SimpleNamespace(resolve=lambda *_args, token=token: token)
            with (
                self.subTest(token=token),
                self.assertRaisesRegex(
                    integration_flow.IntegrationFlowError,
                    "access token is invalid",
                ),
            ):
                integration_flow.resolve_power_integrations("team_1", spec, "read-profile", store, lambda *_: None)

        store = SimpleNamespace(resolve=mock.Mock(side_effect=OSError("offline")))
        with self.assertRaisesRegex(integration_flow.IntegrationFlowError, "could not be resolved"):
            integration_flow.resolve_power_integrations("team_1", spec, "read-profile", store, lambda *_: None)


if __name__ == "__main__":
    unittest.main()
