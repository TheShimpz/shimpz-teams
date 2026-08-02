from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from integrations import broker as integration_broker

SCOPES = ("dns.read", "offline_access", "zone.read")
STATE = "s" * 43
CHALLENGE = "c" * 43
CLAIM = "a" * 64
ACCESS = "access-token-private-123456789"
REFRESH = "refresh-token-private-123456789"
LEASE = f"l1.1999999999.{'b' * 43}.{'c' * 43}.{'d' * 43}"


class Transport:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def request(self, **request) -> integration_broker.BrokerHTTPResponse:
        self.requests.append(request)
        operation = urlsplit(str(request["url"])).path.rsplit("/", 1)[-1]
        if operation == "revoke":
            payload = {"revoked": True}
        else:
            payload = {
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "expires_in": 3600,
                "scopes": list(SCOPES),
                "broker_lease": LEASE,
            }
        return integration_broker.BrokerHTTPResponse(
            200,
            "application/json",
            json.dumps(payload, separators=(",", ":")).encode(),
        )


class OAuthBrokerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = Transport()
        self.client = integration_broker.OAuthBrokerClient(self.transport)

    def test_start_url_is_fixed_and_contains_only_public_pkce_fields(self) -> None:
        url = self.client.authorization_url(
            provider_id="cloudflare",
            state=STATE,
            code_challenge=CHALLENGE,
            scopes=SCOPES,
            callback_mode="loopback",
        )
        parsed = urlsplit(url)
        self.assertEqual(
            (parsed.scheme, parsed.netloc, parsed.path),
            ("https", "shimpz.com", "/api/oauth/cloudflare/start"),
        )
        self.assertEqual(
            parse_qs(parsed.query, strict_parsing=True),
            {
                "state": [STATE],
                "code_challenge": [CHALLENGE],
                "scope": [" ".join(SCOPES)],
                "callback": ["loopback"],
            },
        )
        self.assertNotIn("client", url)
        self.assertEqual(self.transport.requests, [])

    def test_hosted_callback_mode_is_named_and_closed(self) -> None:
        url = self.client.authorization_url(
            provider_id="cloudflare",
            state=STATE,
            code_challenge=CHALLENGE,
            scopes=SCOPES,
            callback_mode="hosted",
        )
        self.assertEqual(parse_qs(urlsplit(url).query)["callback"], ["hosted"])
        with self.assertRaises(integration_broker.OAuthBrokerClientError):
            self.client.authorization_url(
                provider_id="cloudflare",
                state=STATE,
                code_challenge=CHALLENGE,
                scopes=SCOPES,
                callback_mode="https://evil.example",
            )

    def test_fixed_transport_uses_only_the_authenticated_broker_proxy(self) -> None:
        response = Mock(
            status=200,
            read=Mock(return_value=b"{}"),
            getheader=Mock(
                side_effect=lambda name, default=None: "application/json" if name == "Content-Type" else default
            ),
        )
        connection = Mock(getresponse=Mock(return_value=response))
        token = "a" * 64
        capability_path = Path("/run/shimpz-account-egress/token")
        with (
            patch.object(integration_broker.http.client, "HTTPSConnection", return_value=connection) as connect,
            patch.object(
                integration_broker.account_egress,
                "read_capability",
                return_value=token,
            ) as read_capability,
        ):
            transport = integration_broker.FixedBrokerTransport(
                proxy_host="shimpz-account-egress",
                proxy_capability_file=capability_path,
            )
            result = transport.request(
                url="https://shimpz.com/api/oauth/cloudflare/claim",
                headers={"Content-Type": "application/json"},
                body=b"{}",
            )

        self.assertEqual(result.status, 200)
        read_capability.assert_called_once_with(capability_path)
        connect.assert_called_once_with("shimpz-account-egress", 8889, timeout=10)
        tunnel = connection.set_tunnel.call_args
        self.assertEqual(tunnel.args, ("shimpz.com", 443))
        self.assertRegex(tunnel.kwargs["headers"]["Proxy-Authorization"], r"^Basic [A-Za-z0-9+/]+=*$")
        connection.request.assert_called_once_with(
            "POST",
            "/api/oauth/cloudflare/claim",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )

    def test_fixed_transport_rejects_partial_or_foreign_proxy_configuration(self) -> None:
        invalid = (
            {"proxy_host": "shimpz-account-egress"},
            {"proxy_capability_file": "/run/shimpz-account-egress/token"},
            {
                "proxy_host": "evil.example",
                "proxy_capability_file": "/run/shimpz-account-egress/token",
            },
            {
                "proxy_host": "shimpz-account-egress",
                "proxy_capability_file": "relative/token",
            },
        )
        for values in invalid:
            with self.subTest(values=set(values)), self.assertRaises(integration_broker.OAuthBrokerClientError):
                integration_broker.FixedBrokerTransport(**values)

    def test_proxy_capability_reader_rejects_unsafe_file_custody(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                integration_broker.account_egress.grp,
                "getgrnam",
                return_value=Mock(gr_gid=os.getgid()),
            ),
        ):
            path = Path(directory) / "token"
            path.write_text("a" * 64, encoding="ascii")
            path.chmod(0o440)
            self.assertEqual(
                integration_broker.account_egress.read_capability(
                    path,
                    owner_uid=os.getuid(),
                ),
                "a" * 64,
            )
            path.chmod(0o400)
            with self.assertRaises(integration_broker.account_egress.AccountEgressCapabilityError):
                integration_broker.account_egress.read_capability(
                    path,
                    owner_uid=os.getuid(),
                )

    def test_claim_refresh_and_revoke_use_only_fixed_broker_operations(self) -> None:
        claimed = self.client.claim(
            provider_id="cloudflare",
            claim=CLAIM,
            state=STATE,
            code_verifier="v" * 43,
            scopes=SCOPES,
        )
        refreshed = self.client.refresh(
            provider_id="cloudflare",
            refresh_token=REFRESH,
            broker_lease=LEASE,
            scopes=SCOPES,
        )
        self.client.revoke(
            provider_id="cloudflare",
            token=ACCESS,
            broker_lease=LEASE,
        )

        self.assertEqual(claimed.broker_lease, LEASE)
        self.assertEqual(refreshed.access_token, ACCESS)
        self.assertEqual(
            [urlsplit(str(request["url"])).path for request in self.transport.requests],
            [
                "/api/oauth/cloudflare/claim",
                "/api/oauth/cloudflare/refresh",
                "/api/oauth/cloudflare/revoke",
            ],
        )
        self.assertTrue(
            all(
                request["headers"]
                == {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "shimpz-local-controller/1",
                }
                for request in self.transport.requests
            )
        )
        self.assertTrue(all(b"client_secret" not in request["body"] for request in self.transport.requests))

    def test_invalid_provider_and_response_shapes_fail_without_reflection(self) -> None:
        with self.assertRaises(integration_broker.OAuthBrokerClientError):
            self.client.authorization_url(
                provider_id="https://evil.example",
                state=STATE,
                code_challenge=CHALLENGE,
                scopes=SCOPES,
                callback_mode="loopback",
            )

        private = "private-broker-response-123456789"

        class InvalidTransport:
            def request(self, **_request) -> integration_broker.BrokerHTTPResponse:
                return integration_broker.BrokerHTTPResponse(
                    200,
                    "application/json",
                    json.dumps({"unexpected": private}).encode(),
                )

        client = integration_broker.OAuthBrokerClient(InvalidTransport())
        with self.assertRaises(integration_broker.OAuthBrokerClientError) as captured:
            client.claim(
                provider_id="cloudflare",
                claim=CLAIM,
                state=STATE,
                code_verifier="v" * 43,
                scopes=SCOPES,
            )
        self.assertNotIn(private, f"{captured.exception!r} {captured.exception}")


if __name__ == "__main__":
    unittest.main()
