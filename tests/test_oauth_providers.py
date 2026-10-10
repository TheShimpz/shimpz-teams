import unittest

from integrations import providers as integration_providers


class OAuthProviderTests(unittest.TestCase):
    def test_cloudflare_provider_is_core_owned_and_uses_pkce(self) -> None:
        provider = integration_providers.resolve("cloudflare")

        self.assertEqual(provider.pkce_method, "S256")
        self.assertEqual(
            provider.allowed_scopes,
            {"dns.read", "dns.write", "offline_access", "zone.read"},
        )
        self.assertEqual(set(integration_providers.PROVIDERS), {"cloudflare"})
        with self.assertRaises(TypeError):
            integration_providers.PROVIDERS["evil"] = provider

    def test_connection_scopes_are_canonical_and_limited_to_the_trusted_provider(self) -> None:
        intent = integration_providers.integration_intent(
            "cloudflare",
            ("zone.read", "offline_access", "dns.write", "dns.read"),
        )
        self.assertEqual(intent.provider.id, "cloudflare")
        self.assertEqual(
            intent.scopes,
            ("dns.read", "dns.write", "offline_access", "zone.read"),
        )

        invalid = (
            ("unknown", ("zone.read",)),
            ("Cloudflare", ("zone.read",)),
            ("cloudflare", ()),
            ("cloudflare", "zone.read"),
            ("cloudflare", ("zone.read", "zone.read")),
            ("cloudflare", ("zone.write",)),
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
            "api_hosts": ("api.example.com",),
            "allowed_scopes": frozenset({"data.read"}),
            "routes": (("GET", "/v1/items"),),
        }
        self.assertEqual(integration_providers._provider(**base).id, "provider")
        invalid = (
            {"provider_id": "Provider"},
            {"api_hosts": ()},
            {"allowed_scopes": frozenset()},
            {"allowed_scopes": frozenset({"bad/scope"})},
            {"routes": ()},
            {"routes": (("TRACE", "/v1/items"),)},
            {"routes": (("GET", "v1/items"),)},
        )
        for changed in invalid:
            values = {**base, **changed}
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, "registry is invalid"):
                integration_providers._provider(**values)

    def test_the_cloudflare_bearer_reaches_only_the_reviewed_dns_endpoints(self) -> None:
        cloudflare = integration_providers.resolve("cloudflare")
        zone, record = "a" * 32, "b" * 32
        allowed = (
            ("GET", "/client/v4/zones"),
            ("GET", f"/client/v4/zones/{zone}"),
            ("GET", f"/client/v4/zones/{zone}/dns_records"),
            ("GET", f"/client/v4/zones/{zone}/dns_records/{record}"),
            ("POST", f"/client/v4/zones/{zone}/dns_records"),
            ("PUT", f"/client/v4/zones/{zone}/dns_records/{record}"),
            ("DELETE", f"/client/v4/zones/{zone}/dns_records/{record}"),
        )
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                self.assertTrue(cloudflare.allows(method, path))
        refused = (
            # Credential-issuing, credential-inspecting, and account endpoints.
            ("GET", "/client/v4/user/tokens/verify"),
            ("POST", "/client/v4/user/tokens"),
            ("POST", f"/client/v4/accounts/{zone}/tokens"),
            ("GET", "/client/v4/user"),
            ("GET", "/client/v4/accounts"),
            # Zone writes and record endpoints with an unreviewed method.
            ("PATCH", f"/client/v4/zones/{zone}"),
            ("DELETE", f"/client/v4/zones/{zone}"),
            ("POST", "/client/v4/zones"),
            ("PATCH", f"/client/v4/zones/{zone}/dns_records/{record}"),
            ("DELETE", f"/client/v4/zones/{zone}/dns_records"),
            # Non-canonical paths are refused, never normalized.
            ("GET", "/client/v4/zones/"),
            ("GET", "//client/v4/zones"),
            ("GET", f"/client/v4/zones/{zone}/../../user/tokens"),
            ("GET", f"/client/v4/zones/{zone}/%2e%2e/user"),
            ("GET", f"/client/v4/zones/{zone.upper()}"),
            ("GET", f"/client/v4/zones/{zone}%2Fdns_records"),
            ("GET", "/client/v4/zones/x"),
        )
        for method, path in refused:
            with self.subTest(method=method, path=path):
                self.assertFalse(cloudflare.allows(method, path))


if __name__ == "__main__":
    unittest.main()
