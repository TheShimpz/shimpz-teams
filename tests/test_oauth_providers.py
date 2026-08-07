from __future__ import annotations

import unittest

from integrations import providers as integration_providers


class OAuthProviderTests(unittest.TestCase):
    def test_cloudflare_provider_is_core_owned_and_uses_confidential_pkce(self) -> None:
        provider = integration_providers.resolve("cloudflare")

        self.assertEqual(provider.authorization_endpoint, "https://dash.cloudflare.com/oauth2/auth")
        self.assertEqual(provider.token_endpoint, "https://dash.cloudflare.com/oauth2/token")
        self.assertEqual(provider.revocation_endpoint, "https://dash.cloudflare.com/oauth2/revoke")
        self.assertEqual(provider.api_hosts, ("api.cloudflare.com",))
        self.assertEqual(provider.pkce_method, "S256")
        self.assertEqual(provider.client_auth_method, "client_secret_basic")
        self.assertEqual(
            provider.allowed_scopes,
            {"dns.read", "offline_access", "zone.read"},
        )
        self.assertEqual(set(integration_providers.PROVIDERS), {"cloudflare"})
        with self.assertRaises(TypeError):
            integration_providers.PROVIDERS["evil"] = provider

    def test_connection_scopes_are_canonical_and_limited_to_the_trusted_provider(self) -> None:
        intent = integration_providers.integration_intent(
            "cloudflare",
            ("zone.read", "offline_access", "dns.read"),
        )
        self.assertEqual(intent.provider.id, "cloudflare")
        self.assertEqual(
            intent.scopes,
            ("dns.read", "offline_access", "zone.read"),
        )

        invalid = (
            ("unknown", ("zone.read",)),
            ("Cloudflare", ("zone.read",)),
            ("cloudflare", ()),
            ("cloudflare", "zone.read"),
            ("cloudflare", ("zone.read", "zone.read")),
            ("cloudflare", ("dns.write",)),
            ("cloudflare", ("zone/read",)),
            ("cloudflare", tuple("scope" for _ in range(integration_providers.MAX_REQUESTED_SCOPES + 1))),
        )
        for provider_id, scopes in invalid:
            with (
                self.subTest(provider=provider_id, scopes=scopes),
                self.assertRaises(integration_providers.OAuthProviderError),
            ):
                integration_providers.integration_intent(provider_id, scopes)

    def test_trusted_provider_factory_rejects_invalid_registry_metadata(self) -> None:
        base = {
            "provider_id": "provider",
            "authorization_endpoint": "https://provider.example/authorize",
            "token_endpoint": "https://provider.example/token",
            "revocation_endpoint": "https://provider.example/revoke",
            "api_hosts": ("api.provider.example",),
            "allowed_scopes": frozenset({"data.read"}),
            "client_auth_method": "none",
        }
        invalid = (
            {"provider_id": "Provider"},
            {"client_auth_method": "private_key_jwt"},
            {"authorization_endpoint": "http://provider.example/authorize"},
            {"token_endpoint": "https://user@provider.example/token"},
            {"revocation_endpoint": "https://provider.example/revoke?all=true"},
            {"allowed_scopes": frozenset({"bad/scope"})},
        )
        for changed in invalid:
            values = {**base, **changed}
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, "registry is invalid"):
                integration_providers._provider(**values)


if __name__ == "__main__":
    unittest.main()
