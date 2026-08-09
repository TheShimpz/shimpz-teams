from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations import store as integration_store
from integrations.http import OAuthTokenSet

ACCESS = "access-token-private-material-123456789"
REFRESH = "refresh-token-private-material-987654321"
SCOPES = ("dns.read", "offline_access", "zone.read")
DECLARATIONS = {"cloudflare": {"provider": "cloudflare", "scopes": SCOPES}}
ACCOUNT = {"id": "2244994945", "username": "Cloudflare", "name": "Cloudflare"}


def tokens(
    *,
    access: str = ACCESS,
    refresh: str | None = REFRESH,
    scopes: tuple[str, ...] = SCOPES,
) -> OAuthTokenSet:
    return OAuthTokenSet(access, refresh, scopes, 3600)


class OAuthIntegrationReplacementTests(unittest.TestCase):
    def _store(self, root: Path) -> integration_store.OAuthIntegrationStore:
        return integration_store.OAuthIntegrationStore(
            root / "state" / "integrations.json",
            root / "key" / "aes256.key",
            clock=lambda: 1_000_000_000,
        )

    def test_replace_demotes_revokes_then_exchanges_and_preserves_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            first = store.put(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                SCOPES,
                tokens(),
                ACCOUNT,
            )
            widened = tuple(sorted((*SCOPES, "dns.write")))
            declarations = {"cloudflare": {"provider": "cloudflare", "scopes": widened}}
            events: list[str] = []

            def revoke(provider: str, access: str, refresh: str | None, lease: str | None) -> None:
                self.assertEqual((provider, access, refresh, lease), ("cloudflare", ACCESS, REFRESH, None))
                metadata = store.metadata("team_1", "shimpz-cloudflare", declarations)[0]
                self.assertEqual(metadata.status, "reauthorization-required")
                events.append("revoke")

            def exchange() -> OAuthTokenSet:
                self.assertEqual(events, ["revoke"])
                events.append("exchange")
                return tokens(access="replacement-access-token-123456789", scopes=widened)

            replaced = store.replace(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                widened,
                integration_store.OAuthReplacementCallbacks(exchange, revoke),
                ACCOUNT,
            )

            self.assertEqual((first.generation, replaced.generation, events), (1, 2, ["revoke", "exchange"]))
            self.assertEqual(replaced.status, "connected")
            self.assertEqual(store.metadata("team_1", "shimpz-cloudflare", declarations), (replaced,))

    def test_replace_aborts_before_exchange_and_leaves_reauthorization_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.put("team_1", "assistant", "cloudflare", "cloudflare", SCOPES, tokens())
            exchange = mock.Mock(return_value=tokens(access="replacement-access-token-123456789"))
            failure = integration_store.OAuthIntegrationRevocationError("provider unavailable")

            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                store.replace(
                    "team_1",
                    "assistant",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    integration_store.OAuthReplacementCallbacks(exchange, mock.Mock(side_effect=failure)),
                )

            exchange.assert_not_called()
            metadata = store.metadata("team_1", "assistant", DECLARATIONS)[0]
            self.assertEqual((metadata.status, metadata.generation), ("reauthorization-required", 1))
            with self.assertRaises(integration_store.OAuthIntegrationReauthorizationError):
                store.resolve("team_1", "assistant", "cloudflare", "cloudflare", SCOPES, mock.Mock())

    def test_replace_rejects_invalid_callback_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            invalid = (
                None,
                integration_store.OAuthReplacementCallbacks(None, mock.Mock()),
                integration_store.OAuthReplacementCallbacks(mock.Mock(), None),
            )
            for callbacks in invalid:
                with self.subTest(callbacks=callbacks), self.assertRaises(
                    integration_store.OAuthIntegrationValidationError
                ):
                    store.replace("team_1", "assistant", "cloudflare", "cloudflare", SCOPES, callbacks)

    def test_exchange_failure_is_retryable_and_put_failure_revokes_new_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.put("team_1", "assistant", "cloudflare", "cloudflare", SCOPES, tokens())
            revocations: list[str] = []

            def revoke(_provider: str, access: str, _refresh: str | None, _lease: str | None) -> None:
                revocations.append(access)

            with self.assertRaisesRegex(RuntimeError, "exchange failed"):
                store.replace(
                    "team_1",
                    "assistant",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    integration_store.OAuthReplacementCallbacks(
                        lambda: (_ for _ in ()).throw(RuntimeError("exchange failed")),
                        revoke,
                    ),
                )
            self.assertEqual(revocations, [ACCESS])
            self.assertEqual(store.metadata("team_1", "assistant", DECLARATIONS)[0].status, "reauthorization-required")

            replacement = tokens(access="replacement-access-token-123456789")
            with (
                mock.patch.object(
                    store,
                    "put",
                    side_effect=integration_store.OAuthIntegrationStoreError("write failed"),
                ),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "write failed"),
            ):
                store.replace(
                    "team_1",
                    "assistant",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    integration_store.OAuthReplacementCallbacks(lambda: replacement, revoke),
                )
            self.assertEqual(revocations, [ACCESS, ACCESS, "replacement-access-token-123456789"])

    def test_replace_preserves_write_failure_when_compensation_revocation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            replacement = tokens(access="replacement-access-token-123456789")
            with (
                mock.patch.object(
                    store,
                    "put",
                    side_effect=integration_store.OAuthIntegrationStoreError("write failed"),
                ),
                self.assertLogs("shimpz.team.integrations.store", level="ERROR"),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "write failed"),
            ):
                store.replace(
                    "team_1",
                    "assistant",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    integration_store.OAuthReplacementCallbacks(
                        lambda: replacement,
                        mock.Mock(side_effect=integration_store.OAuthIntegrationRevocationError("unavailable")),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
