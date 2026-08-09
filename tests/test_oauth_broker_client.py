from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from integrations import broker as integration_broker

SCOPES = ("dns.read", "offline_access", "zone.read")
STATE = "s" * 43
CHALLENGE = "c" * 43
CLAIM = "a" * 64
ACCESS = "access-token-private-123456789"
REFRESH = "refresh-token-private-123456789"
LEASE = f"l2.1999999999.{'b' * 43}.{'c' * 43}.{'d' * 43}.{'e' * 43}"


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
        out_of_band = self.client.authorization_url(
            provider_id="cloudflare",
            state=STATE,
            code_challenge=CHALLENGE,
            scopes=SCOPES,
            callback_mode="out-of-band",
        )
        self.assertEqual(parse_qs(urlsplit(out_of_band).query)["callback"], ["out-of-band"])
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

    def test_proxy_capability_reader_rejects_change_encoding_and_value_edges(self) -> None:
        account_egress = integration_broker.account_egress
        metadata = Mock(
            st_mode=0o100440,
            st_nlink=1,
            st_uid=0,
            st_gid=7,
            st_size=64,
            st_dev=1,
            st_ino=1,
            st_mtime_ns=1,
            st_ctime_ns=1,
        )
        changed = Mock(
            st_mode=0o100440,
            st_nlink=1,
            st_uid=0,
            st_gid=7,
            st_size=64,
            st_dev=1,
            st_ino=2,
            st_mtime_ns=1,
            st_ctime_ns=1,
        )
        for raw, after, message in (
            (b"a" * 63, metadata, "changed while reading"),
            (b"a" * 64, changed, "changed while reading"),
            (b"\xff" * 64, metadata, "invalid"),
            (b"g" * 64, metadata, "invalid"),
        ):
            with (
                self.subTest(message=message),
                patch.object(account_egress.grp, "getgrnam", return_value=Mock(gr_gid=7)),
                patch.object(account_egress.os, "open", return_value=3),
                patch.object(account_egress.os, "fstat", side_effect=(metadata, after)),
                patch.object(account_egress.os, "read", return_value=raw),
                patch.object(account_egress.os, "close"),
                self.assertRaisesRegex(account_egress.AccountEgressCapabilityError, message),
            ):
                account_egress.read_capability(Path("/token"))

        with (
            patch.object(account_egress.grp, "getgrnam", return_value=Mock(gr_gid=7)),
            patch.object(account_egress.os, "open", return_value=-1),
            patch.object(account_egress.os, "fstat", return_value=metadata),
            patch.object(account_egress.os, "read", return_value=b"a" * 64),
            patch.object(account_egress.os, "close") as close,
        ):
            self.assertEqual(account_egress.read_capability(Path("/token")), "a" * 64)
        close.assert_not_called()

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
        with self.assertRaises(integration_broker.OAuthBrokerClientError):
            self.client.revoke(provider_id="unknown", token=ACCESS, broker_lease=LEASE)

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

    def test_direct_transport_endpoint_capability_and_io_edges_are_closed(self) -> None:
        transport = integration_broker.FixedBrokerTransport()
        for url in (
            "http://shimpz.com/api/oauth/cloudflare/claim",
            "https://evil.example/api/oauth/cloudflare/claim",
            "https://shimpz.com/api/oauth/cloudflare/start",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(
                    integration_broker.OAuthBrokerClientError,
                    "endpoint is invalid",
                ),
            ):
                transport.request(url=url, headers={}, body=b"{}")

        response = Mock(status=200, read=Mock(return_value=b"{}"), getheader=Mock(return_value="application/json"))
        connection = Mock(getresponse=Mock(return_value=response))
        with patch.object(integration_broker.http.client, "HTTPSConnection", return_value=connection) as connect:
            self.assertEqual(
                transport.request(
                    url="https://shimpz.com/api/oauth/cloudflare/claim",
                    headers={},
                    body=b"{}",
                ).body,
                b"{}",
            )
        connect.assert_called_once_with("shimpz.com", timeout=integration_broker.HTTP_TIMEOUT_SECONDS)
        connection.close.assert_called_once()

        proxied = integration_broker.FixedBrokerTransport(
            proxy_host="shimpz-account-egress",
            proxy_capability_file="/run/token",
        )
        with (
            patch.object(
                integration_broker.account_egress,
                "read_capability",
                side_effect=integration_broker.account_egress.AccountEgressCapabilityError("missing"),
            ),
            self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "capability is unavailable"),
        ):
            proxied.request(
                url="https://shimpz.com/api/oauth/cloudflare/claim",
                headers={},
                body=b"{}",
            )

        for payload, failure in (
            (b"x" * (integration_broker.MAX_RESPONSE_BYTES + 1), None),
            (b"{}", OSError("offline")),
        ):
            response = Mock(
                status=200,
                read=Mock(return_value=payload),
                getheader=Mock(return_value="application/json"),
            )
            connection = Mock(getresponse=Mock(return_value=response))
            if failure is not None:
                connection.request.side_effect = failure
            with (
                self.subTest(failure=failure),
                patch.object(integration_broker.http.client, "HTTPSConnection", return_value=connection),
                self.assertRaises(integration_broker.OAuthBrokerClientError),
            ):
                transport.request(
                    url="https://shimpz.com/api/oauth/cloudflare/claim",
                    headers={},
                    body=b"{}",
                )
            connection.close.assert_called_once()

    def test_broker_helper_and_response_contract_edges_fail_closed(self) -> None:
        self.assertEqual(repr(integration_broker.OAuthBrokerClient(self.transport)), "<OAuthBrokerClient shimpz.com>")
        for value in (None, "short"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "binding"),
            ):
                integration_broker._binding(value)
        for value in (None, "é" * 20, "x" * 15, "x" * 19 + "\n"):
            with self.subTest(value=value), self.assertRaises(integration_broker.OAuthBrokerClientError):
                integration_broker._private_text(value, "secret")

        foreign = SimpleNamespace(provider=SimpleNamespace(id="foreign"), scopes=("read",))
        with (
            patch.object(integration_broker.integration_providers, "integration_intent", return_value=foreign),
            self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "provider is unavailable"),
        ):
            integration_broker._intent("foreign", ("read",))

        invalid_responses = (
            integration_broker.BrokerHTTPResponse(503, "application/json", b"{}"),
            integration_broker.BrokerHTTPResponse(200, "text/plain", b"{}"),
            integration_broker.BrokerHTTPResponse(200, "application/json", b""),
            integration_broker.BrokerHTTPResponse(200, "application/json", b"{"),
            integration_broker.BrokerHTTPResponse(200, "application/json", b"[]"),
        )
        for response in invalid_responses:
            with self.subTest(response=response), self.assertRaises(integration_broker.OAuthBrokerClientError):
                integration_broker._object(response)

        with self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "operation is invalid"):
            self.client._post("start", {})

    def test_token_claim_lease_and_revoke_result_edges_fail_closed(self) -> None:
        valid = {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "expires_in": 3600,
            "scopes": list(SCOPES),
            "broker_lease": LEASE,
        }
        for changed in (
            {"extra": True},
            {"expires_in": True},
            {"scopes": []},
            {"broker_lease": "invalid"},
        ):
            value = {**valid, **changed}
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(
                    integration_broker.OAuthBrokerClientError,
                    "response is invalid",
                ),
            ):
                self.client._tokens(value, SCOPES)

        with self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "claim is invalid"):
            self.client.claim(
                provider_id="cloudflare",
                claim="invalid",
                state=STATE,
                code_verifier="v" * 43,
                scopes=SCOPES,
            )
        with self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "lease is invalid"):
            self.client.refresh(
                provider_id="cloudflare",
                refresh_token=REFRESH,
                broker_lease="invalid",
                scopes=SCOPES,
            )
        with self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "lease is invalid"):
            self.client.revoke(provider_id="cloudflare", token=ACCESS, broker_lease="invalid")
        with (
            patch.object(
                integration_broker.integration_providers,
                "resolve",
                return_value=SimpleNamespace(id="foreign"),
            ),
            self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "provider is unavailable"),
        ):
            self.client.revoke(provider_id="foreign", token=ACCESS, broker_lease=LEASE)

        class BadRevokeTransport(Transport):
            def request(self, **request) -> integration_broker.BrokerHTTPResponse:
                response = super().request(**request)
                return integration_broker.BrokerHTTPResponse(
                    response.status,
                    response.content_type,
                    b'{"revoked":false}',
                )

        with self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "response is invalid"):
            integration_broker.OAuthBrokerClient(BadRevokeTransport()).revoke(
                provider_id="cloudflare",
                token=ACCESS,
                broker_lease=LEASE,
            )


if __name__ == "__main__":
    unittest.main()
