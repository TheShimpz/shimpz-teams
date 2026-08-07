from __future__ import annotations

import json
import unittest
from base64 import b64decode
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from integrations import http as integration_http

CLIENT_ID = "cloudflare-client-id-123"
CLIENT_CREDENTIAL = "cloudflare-client-secret-123"
CODE = "authorization-code-123456789"
VERIFIER = "v" * 64
STATE = "s" * 43
CHALLENGE = "c" * 43
SCOPES = ("dns.read", "offline_access", "zone.read")
ACCESS = "access-token-123456789"
REFRESH = "refresh-token-123456789"


@dataclass
class FakeTransport:
    responses: list[integration_http.OAuthHTTPResponse]
    requests: list[dict[str, object]] = field(default_factory=list)

    def request(self, **request: object) -> integration_http.OAuthHTTPResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def response(payload: object, *, status: int = 200, content_type: str = "application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return integration_http.OAuthHTTPResponse(status, content_type, body)


def _assert_basic(request: dict[str, object]) -> None:
    headers = request["headers"]
    assert isinstance(headers, dict)
    scheme, encoded = headers["Authorization"].split(" ", 1)
    if scheme != "Basic" or b64decode(encoded).decode("ascii") != f"{CLIENT_ID}:{CLIENT_CREDENTIAL}":
        raise AssertionError("invalid confidential client authentication")


class OAuthHTTPClientTests(unittest.TestCase):
    def test_authorization_url_is_fixed_cloudflare_pkce_and_exact_redirect(self) -> None:
        url = integration_http.authorization_url(
            provider_id="cloudflare",
            client_id=CLIENT_ID,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
            state=STATE,
            code_challenge=CHALLENGE,
            scopes=SCOPES,
        )
        parsed = urlsplit(url)
        self.assertEqual(
            (parsed.scheme, parsed.netloc, parsed.path),
            ("https", "dash.cloudflare.com", "/oauth2/auth"),
        )
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "response_type": ["code"],
                "client_id": [CLIENT_ID],
                "redirect_uri": [integration_http.HOSTED_REDIRECT_URI],
                "scope": ["dns.read offline_access zone.read"],
                "state": [STATE],
                "code_challenge": [CHALLENGE],
                "code_challenge_method": ["S256"],
            },
        )
        for redirect in (
            "http://127.0.0.1:7777/api/oauth/cloudflare/callback",
            "http://localhost:7777/api/oauth/cloudflare/callback",
            "https://evil.test/callback",
        ):
            with self.subTest(redirect=redirect), self.assertRaises(integration_http.OAuthHTTPError):
                integration_http.authorization_url(
                    provider_id="cloudflare",
                    client_id=CLIENT_ID,
                    redirect_uri=redirect,
                    state=STATE,
                    code_challenge=CHALLENGE,
                    scopes=SCOPES,
                )

    def test_exchange_uses_basic_secret_fixed_endpoint_and_validates_tokens(self) -> None:
        transport = FakeTransport(
            [
                response(
                    {
                        "token_type": "bearer",
                        "access_token": ACCESS,
                        "refresh_token": REFRESH,
                        "expires_in": 7200,
                        "scope": "zone.read dns.read offline_access",
                    }
                )
            ]
        )
        tokens = integration_http.OAuthHTTPClient(transport).exchange_code(
            provider_id="cloudflare",
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
            code=CODE,
            code_verifier=VERIFIER,
            scopes=SCOPES,
        )

        self.assertEqual(tokens.access_token, ACCESS)
        self.assertEqual(tokens.refresh_token, REFRESH)
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://dash.cloudflare.com/oauth2/token")
        _assert_basic(request)
        fields = parse_qs(bytes(request["body"]).decode())
        self.assertNotIn("client_id", fields)
        self.assertNotIn("client_secret", fields)
        self.assertEqual(fields["code_verifier"], [VERIFIER])

    def test_refresh_and_revoke_reuse_confidential_fixed_provider_endpoints(self) -> None:
        transport = FakeTransport(
            [
                response(
                    {
                        "token_type": "Bearer",
                        "access_token": "new-access-token-123456789",
                        "expires_in": 3600,
                        "scope": " ".join(SCOPES),
                    }
                ),
                response(b"", content_type=""),
            ]
        )
        client = integration_http.OAuthHTTPClient(transport)
        tokens = client.refresh(
            provider_id="cloudflare",
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            refresh_token=REFRESH,
            scopes=SCOPES,
        )
        self.assertEqual(tokens.refresh_token, REFRESH)
        client.revoke(
            provider_id="cloudflare",
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            token=tokens.refresh_token,
        )

        self.assertEqual(transport.requests[0]["url"], "https://dash.cloudflare.com/oauth2/token")
        self.assertEqual(transport.requests[1]["url"], "https://dash.cloudflare.com/oauth2/revoke")
        for request in transport.requests:
            _assert_basic(request)
            self.assertNotIn("client_secret", parse_qs(bytes(request["body"]).decode()))
        self.assertEqual(parse_qs(bytes(transport.requests[1]["body"]).decode()), {"token": [REFRESH]})

    def test_redirects_malformed_json_scope_widening_and_reflection_fail_closed(self) -> None:
        bad_responses = (
            response({"error": "do-not-reflect-this-secret"}, status=302),
            response(b'{"access_token":"one","access_token":"two"}'),
            response(
                {
                    "token_type": "bearer",
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "expires_in": 7200,
                    "scope": "dns.read offline_access zone.read dns.write",
                }
            ),
            response(
                {
                    "token_type": "bearer",
                    "access_token": ACCESS,
                    "expires_in": 7200,
                    "scope": " ".join(SCOPES),
                }
            ),
        )
        for provider_response in bad_responses:
            transport = FakeTransport([provider_response])
            with (
                self.subTest(body=provider_response.body),
                self.assertRaises(integration_http.OAuthHTTPError) as caught,
            ):
                integration_http.OAuthHTTPClient(transport).exchange_code(
                    provider_id="cloudflare",
                    client_id=CLIENT_ID,
                    client_secret=CLIENT_CREDENTIAL,
                    redirect_uri=integration_http.HOSTED_REDIRECT_URI,
                    code=CODE,
                    code_verifier=VERIFIER,
                    scopes=SCOPES,
                )
            for private in ("do-not-reflect", ACCESS, CLIENT_CREDENTIAL):
                self.assertNotIn(private, str(caught.exception))

    def test_inputs_and_response_size_are_bounded(self) -> None:
        transport = FakeTransport(
            [
                integration_http.OAuthHTTPResponse(
                    200,
                    "application/json",
                    b"x" * (integration_http.MAX_RESPONSE_BYTES + 1),
                )
            ]
        )
        with self.assertRaises(integration_http.OAuthHTTPError):
            integration_http.OAuthHTTPClient(transport).exchange_code(
                provider_id="cloudflare",
                client_id=CLIENT_ID,
                client_secret=CLIENT_CREDENTIAL,
                redirect_uri=integration_http.HOSTED_REDIRECT_URI,
                code=CODE,
                code_verifier=VERIFIER,
                scopes=SCOPES,
            )
        for invalid_client in ("short", "secret value", "x" * 257):
            with self.subTest(client=invalid_client), self.assertRaises(integration_http.OAuthHTTPError):
                integration_http.authorization_url(
                    provider_id="cloudflare",
                    client_id=invalid_client,
                    redirect_uri=integration_http.HOSTED_REDIRECT_URI,
                    state=STATE,
                    code_challenge=CHALLENGE,
                    scopes=SCOPES,
                )
        for invalid_secret in ("short", "secret value", "x" * 1025):
            with self.subTest(secret=invalid_secret), self.assertRaises(integration_http.OAuthHTTPError):
                integration_http.OAuthHTTPClient(FakeTransport([])).refresh(
                    provider_id="cloudflare",
                    client_id=CLIENT_ID,
                    client_secret=invalid_secret,
                    refresh_token=REFRESH,
                    scopes=SCOPES,
                )

    def test_fixed_https_transport_validates_endpoint_io_and_response_bounds(self) -> None:
        transport = integration_http.FixedHTTPSTransport()
        for url in (
            "http://provider.test/token",
            "https://user@provider.test/token",
            "https://provider.test:443/token",
            "https://provider.test/token?query=1",
            "https://provider.test/token#fragment",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(integration_http.OAuthHTTPError, "endpoint"):
                transport.request(method="POST", url=url, headers={}, body=b"")

        provider_response = SimpleNamespace(
            status=200,
            read=lambda _maximum: b"{}",
            getheader=lambda _name, _default: "application/json",
        )
        connection = mock.Mock()
        connection.getresponse.return_value = provider_response
        with mock.patch.object(integration_http.http.client, "HTTPSConnection", return_value=connection):
            result = transport.request(
                method="POST",
                url="https://provider.test/token",
                headers={"Accept": "application/json"},
                body=b"body",
            )
        self.assertEqual(result, integration_http.OAuthHTTPResponse(200, "application/json", b"{}"))
        connection.close.assert_called_once_with()

        oversized = SimpleNamespace(
            status=200,
            read=lambda _maximum: b"x" * (integration_http.MAX_RESPONSE_BYTES + 1),
            getheader=lambda _name, _default: "application/json",
        )
        connection = mock.Mock()
        connection.getresponse.return_value = oversized
        with (
            mock.patch.object(integration_http.http.client, "HTTPSConnection", return_value=connection),
            self.assertRaisesRegex(integration_http.OAuthHTTPError, "response"),
        ):
            transport.request(method="POST", url="https://provider.test/token", headers={}, body=b"")
        connection.close.assert_called_once_with()

        connection = mock.Mock()
        connection.request.side_effect = OSError("offline")
        with (
            mock.patch.object(integration_http.http.client, "HTTPSConnection", return_value=connection),
            self.assertRaisesRegex(integration_http.OAuthHTTPError, "unavailable"),
        ):
            transport.request(method="POST", url="https://provider.test/token", headers={}, body=b"")
        connection.close.assert_called_once_with()

    def test_http_scalar_challenge_and_provider_guards_fail_closed(self) -> None:
        for function, value in (
            (integration_http._client_secret, None),
            (integration_http._client_secret, "é" * 16),
            (integration_http._authorization_code, None),
            (integration_http._authorization_code, "é" * 16),
            (integration_http._authorization_code, "short"),
            (integration_http._token, None),
            (integration_http._token, "é" * 16),
            (integration_http._token, "short"),
        ):
            with (
                self.subTest(function=function.__name__, value=value),
                self.assertRaises(integration_http.OAuthHTTPError),
            ):
                function(value)

        for payload in (b"", b"[]"):
            with self.subTest(payload=payload), self.assertRaises(integration_http.OAuthHTTPError):
                integration_http._strict_object(payload)
        with self.assertRaisesRegex(integration_http.OAuthHTTPError, "unavailable"):
            integration_http._confidential_provider("missing")
        provider = SimpleNamespace(client_auth_method="none", pkce_method="S256")
        with (
            mock.patch.object(integration_http.integration_providers, "resolve", return_value=provider),
            self.assertRaisesRegex(integration_http.OAuthHTTPError, "configuration"),
        ):
            integration_http._confidential_provider("cloudflare")

        for state, challenge, scopes in (
            ("short", CHALLENGE, SCOPES),
            (STATE, "short", SCOPES),
            (STATE, CHALLENGE, ("invalid",)),
        ):
            with self.subTest(state=state, challenge=challenge), self.assertRaises(integration_http.OAuthHTTPError):
                integration_http.authorization_url(
                    provider_id="cloudflare",
                    client_id=CLIENT_ID,
                    redirect_uri=integration_http.HOSTED_REDIRECT_URI,
                    state=state,
                    code_challenge=challenge,
                    scopes=scopes,
                )

    def test_token_response_variants_and_exchange_scope_guards_fail_closed(self) -> None:
        expected = tuple(sorted(SCOPES))
        inherited = integration_http.OAuthHTTPClient._tokens(
            response(
                {
                    "token_type": "bearer",
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "expires_in": 30,
                }
            ),
            expected_scopes=expected,
        )
        self.assertEqual(inherited.scopes, expected)

        bad_responses = (
            response({}, content_type="text/plain"),
            response({"token_type": "invalid", "expires_in": 30}),
            response(
                {
                    "token_type": "bearer",
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "expires_in": 30,
                    "scope": [],
                }
            ),
        )
        for provider_response in bad_responses:
            with self.subTest(response=provider_response), self.assertRaises(integration_http.OAuthHTTPError):
                integration_http.OAuthHTTPClient._tokens(provider_response, expected_scopes=expected)

        for operation, kwargs in (
            (
                "exchange_code",
                {
                    "provider_id": "cloudflare",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_CREDENTIAL,
                    "redirect_uri": integration_http.HOSTED_REDIRECT_URI,
                    "code": CODE,
                    "code_verifier": "v" * 42,
                    "scopes": SCOPES,
                },
            ),
            (
                "exchange_code",
                {
                    "provider_id": "cloudflare",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_CREDENTIAL,
                    "redirect_uri": integration_http.HOSTED_REDIRECT_URI,
                    "code": CODE,
                    "code_verifier": VERIFIER,
                    "scopes": ("invalid",),
                },
            ),
            (
                "refresh",
                {
                    "provider_id": "cloudflare",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_CREDENTIAL,
                    "refresh_token": REFRESH,
                    "scopes": ("invalid",),
                },
            ),
        ):
            with self.subTest(operation=operation), self.assertRaises(integration_http.OAuthHTTPError):
                getattr(integration_http.OAuthHTTPClient(FakeTransport([])), operation)(**kwargs)

        with self.assertRaisesRegex(integration_http.OAuthHTTPError, "response"):
            integration_http.OAuthHTTPClient(FakeTransport([response(b"body", content_type="text/html")])).revoke(
                provider_id="cloudflare",
                client_id=CLIENT_ID,
                client_secret=CLIENT_CREDENTIAL,
                token=REFRESH,
            )


if __name__ == "__main__":
    unittest.main()
