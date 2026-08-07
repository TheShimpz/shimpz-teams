from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
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
    expires_in: int = 3600,
    broker_lease: str | None = None,
) -> OAuthTokenSet:
    return OAuthTokenSet(access, refresh, scopes, expires_in, broker_lease)


class OAuthIntegrationStoreTests(unittest.TestCase):
    def _store(
        self,
        root: Path,
        *,
        clock=lambda: 1_000_000_000,
    ) -> integration_store.OAuthIntegrationStore:
        return integration_store.OAuthIntegrationStore(
            root / "state" / "integrations.json",
            root / "key" / "aes256.key",
            clock=clock,
        )

    def test_inventory_includes_missing_and_encrypted_integration_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            missing = store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)
            self.assertEqual(
                missing,
                (
                    integration_store.OAuthIntegrationMetadata(
                        "cloudflare", "cloudflare", SCOPES, "missing", None, None, 0
                    ),
                ),
            )

            stored = store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            self.assertEqual(stored.generation, 1)
            self.assertEqual(stored.status, "connected")
            self.assertEqual(stored.integration, integration_store.OAuthIntegrationIdentity(**ACCOUNT))
            self.assertEqual(store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS), (stored,))
            self.assertEqual(
                store.resolve(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    lambda _token, _lease: self.fail("unexpired token must not refresh"),
                ),
                ACCESS,
            )

            state = (root / "state" / "integrations.json").read_text(encoding="utf-8")
            key = (root / "key" / "aes256.key").read_bytes()
            for private in (ACCESS, REFRESH, "2244994945", "Cloudflare", "Cloudflare"):
                self.assertNotIn(private, state)
                self.assertNotIn(private.encode(), key)
            self.assertNotIn("access_token", state)
            self.assertNotIn("refresh_token", state)
            self.assertNotIn(ACCESS, repr(stored))
            self.assertEqual(stat.S_IMODE(store.state_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.key_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.state_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.key_path.parent.stat().st_mode), 0o700)

    def test_resolve_reuses_only_the_validated_state_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)

            with mock.patch.object(
                integration_store,
                "_validate_state",
                wraps=integration_store._validate_state,
            ) as validate_state:
                for _ in range(2):
                    self.assertEqual(
                        store.resolve(
                            "team_1",
                            "shimpz-cloudflare",
                            "cloudflare",
                            "cloudflare",
                            SCOPES,
                            lambda *_args: self.fail("unexpired token must not refresh"),
                        ),
                        ACCESS,
                    )

            self.assertEqual(validate_state.call_count, 1)

    def test_external_atomic_replacement_invalidates_cached_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._store(root)
            second = self._store(root)
            first.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            self.assertEqual(
                first.resolve(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    lambda *_args: self.fail("unexpired token must not refresh"),
                ),
                ACCESS,
            )

            replacement = "replacement-access-token-private-material"
            second.put(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                SCOPES,
                tokens(access=replacement),
                ACCOUNT,
            )

            self.assertEqual(
                first.resolve(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    lambda *_args: self.fail("unexpired token must not refresh"),
                ),
                replacement,
            )

    def test_mutation_never_aliases_the_cached_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)
            cached = store._state_cache
            self.assertIsNotNone(cached)

            self.assertTrue(store.delete_integration("team_1", "shimpz-cloudflare", "cloudflare"))

            if cached is None:
                self.fail("metadata read did not populate the state cache")
            records = integration_store._PRIVATE_STATE.records(
                cached,
                "team_1",
                "shimpz-cloudflare",
                create=False,
            )
            self.assertIn("cloudflare", records)

    def test_failed_write_drops_the_validated_state_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)
            self.assertIsNotNone(store._state_cache)

            with (
                mock.patch.object(
                    integration_store.private_state.PrivateState,
                    "atomic_write",
                    side_effect=integration_store.OAuthIntegrationStoreError("write failed"),
                ),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "write failed"),
            ):
                store.put(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    tokens(access="replacement-access-token-private-material"),
                    ACCOUNT,
                )

            self.assertIsNone(store._state_cache)
            self.assertEqual(
                store.resolve(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    lambda *_args: self.fail("unexpired token must not refresh"),
                ),
                ACCESS,
            )

    def test_rotation_is_atomic_and_increments_authenticated_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            writes = 0
            original = store._write_state

            def counted(state) -> None:
                nonlocal writes
                writes += 1
                original(state)

            store._write_state = counted
            first = store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            second = store.put(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                SCOPES,
                tokens(access="new-access-token-123456789"),
                ACCOUNT,
            )
            self.assertEqual((first.generation, second.generation, writes), (1, 2, 2))
            self.assertEqual(
                store.resolve(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    lambda _token, _lease: None,
                ),
                "new-access-token-123456789",
            )

    def test_expired_integration_refresh_is_single_flight_and_preserves_integration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000]
            store = self._store(Path(directory), clock=lambda: now[0])
            store.put(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                SCOPES,
                tokens(expires_in=30, broker_lease="broker-lease-private-material-123456789"),
                ACCOUNT,
            )
            self.assertEqual(
                store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)[0].status,
                "connected",
            )
            now[0] = 1_031
            self.assertEqual(
                store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)[0].status,
                "refresh-required",
            )

            entered = threading.Event()
            release = threading.Event()
            calls: list[str] = []

            def refresh(value: str, lease: str | None) -> OAuthTokenSet:
                calls.append(value)
                self.assertEqual(lease, "broker-lease-private-material-123456789")
                entered.set()
                self.assertTrue(release.wait(2))
                return tokens(access="refreshed-access-token-123456789", expires_in=3600)

            def resolve() -> str:
                return store.resolve("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, refresh)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(resolve)
                self.assertTrue(entered.wait(2))
                second = pool.submit(resolve)
                release.set()
                self.assertEqual(first.result(2), "refreshed-access-token-123456789")
                self.assertEqual(second.result(2), "refreshed-access-token-123456789")
            self.assertEqual(calls, [REFRESH])
            metadata = store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)[0]
            self.assertEqual(metadata.generation, 2)
            self.assertEqual(metadata.integration, integration_store.OAuthIntegrationIdentity(**ACCOUNT))

    def test_hanging_refresh_does_not_block_another_integration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000]
            store = self._store(Path(directory), clock=lambda: now[0])
            store.put(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                SCOPES,
                tokens(expires_in=30),
                ACCOUNT,
            )
            other_access = "other-access-token-private-material-123456789"
            store.put(
                "team_1",
                "shimpz-cloudflare",
                "secondary",
                "cloudflare",
                SCOPES,
                tokens(access=other_access),
                ACCOUNT,
            )
            now[0] = 1_031
            entered = threading.Event()
            release = threading.Event()

            def refresh(_token: str, _lease: str | None) -> OAuthTokenSet:
                entered.set()
                self.assertTrue(release.wait(2))
                return tokens(access="refreshed-access-token-123456789")

            with ThreadPoolExecutor(max_workers=2) as pool:
                refreshing = pool.submit(
                    store.resolve,
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    refresh,
                )
                self.assertTrue(entered.wait(1))
                other = pool.submit(
                    store.resolve,
                    "team_1",
                    "shimpz-cloudflare",
                    "secondary",
                    "cloudflare",
                    SCOPES,
                    lambda *_args: self.fail("unexpired secondary integration must not refresh"),
                )
                try:
                    self.assertEqual(other.result(timeout=0.5), other_access)
                finally:
                    release.set()
                self.assertEqual(refreshing.result(timeout=2), "refreshed-access-token-123456789")
            self.assertEqual(store._integration_flights, {})

    def test_hanging_revocation_does_not_block_another_integration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            other_access = "other-access-token-private-material-123456789"
            store.put(
                "team_1",
                "shimpz-cloudflare",
                "secondary",
                "cloudflare",
                SCOPES,
                tokens(access=other_access),
                ACCOUNT,
            )
            entered = threading.Event()
            release = threading.Event()

            def revoke(*_tokens) -> None:
                entered.set()
                self.assertTrue(release.wait(2))

            with ThreadPoolExecutor(max_workers=2) as pool:
                revoking = pool.submit(
                    store.revoke_then_delete,
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    revoke,
                )
                self.assertTrue(entered.wait(1))
                other = pool.submit(
                    store.resolve,
                    "team_1",
                    "shimpz-cloudflare",
                    "secondary",
                    "cloudflare",
                    SCOPES,
                    lambda *_args: self.fail("unexpired secondary integration must not refresh"),
                )
                try:
                    self.assertEqual(other.result(timeout=0.5), other_access)
                finally:
                    release.set()
                self.assertTrue(revoking.result(timeout=2))
            self.assertEqual(store._integration_flights, {})

    def test_missing_refresh_and_declaration_drift_require_reauthorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000]
            store = self._store(Path(directory), clock=lambda: now[0])
            reduced_scopes = ("dns.read", "zone.read")
            store.put(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                reduced_scopes,
                tokens(refresh=None, scopes=reduced_scopes, expires_in=30),
                None,
            )
            drifted = store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)[0]
            self.assertEqual(drifted.status, "reauthorization-required")
            self.assertEqual(drifted.scopes, SCOPES)
            self.assertIsNone(drifted.integration)
            with self.assertRaises(integration_store.OAuthIntegrationReauthorizationError):
                store.resolve(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    lambda _token, _lease: None,
                )

            reduced = {"cloudflare": {"provider": "cloudflare", "scopes": reduced_scopes}}
            now[0] = 1_031
            self.assertEqual(
                store.metadata("team_1", "shimpz-cloudflare", reduced)[0].status,
                "reauthorization-required",
            )
            with self.assertRaises(integration_store.OAuthIntegrationReauthorizationError):
                store.resolve(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    reduced_scopes,
                    lambda _token, _lease: None,
                )

    def test_aad_rejects_cross_identity_copy_and_metadata_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            store.put(
                "team_2",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                SCOPES,
                tokens(access="other-access-token-123456789"),
                ACCOUNT,
            )
            state_path = root / "state" / "integrations.json"
            original = json.loads(state_path.read_text(encoding="utf-8"))
            copied = json.loads(json.dumps(original))
            copied["teams"]["team_2"]["shimpz-cloudflare"]["cloudflare"] = copied["teams"]["team_1"][
                "shimpz-cloudflare"
            ]["cloudflare"]
            state_path.write_text(json.dumps(copied, separators=(",", ":")), encoding="utf-8")
            state_path.chmod(0o600)
            with self.assertRaises(integration_store.OAuthIntegrationStoreError):
                store.metadata("team_2", "shimpz-cloudflare", DECLARATIONS)

            for field, value in (
                ("expires_at", 1_000_003_601),
                ("status", "reauthorization-required"),
                ("generation", 2),
                ("scopes", ["dns.read", "zone.read"]),
            ):
                tampered = json.loads(json.dumps(original))
                tampered["teams"]["team_1"]["shimpz-cloudflare"]["cloudflare"][field] = value
                state_path.write_text(json.dumps(tampered, separators=(",", ":")), encoding="utf-8")
                state_path.chmod(0o600)
                with self.subTest(field=field), self.assertRaises(integration_store.OAuthIntegrationStoreError):
                    store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)

    def test_missing_or_substituted_key_fails_closed_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            original_state = store.state_path.read_bytes()
            store.key_path.unlink()
            with self.assertRaises(integration_store.OAuthIntegrationStoreError):
                store.put(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    SCOPES,
                    tokens(access="replacement-token-123456789"),
                    ACCOUNT,
                )
            self.assertFalse(store.key_path.exists())
            self.assertEqual(store.state_path.read_bytes(), original_state)

            store.key_path.write_bytes(os.urandom(32))
            store.key_path.chmod(0o600)
            with self.assertRaises(integration_store.OAuthIntegrationStoreError):
                store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)

    def test_invalid_tokens_permissions_symlinks_and_duplicate_json_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            original = store.state_path.read_bytes()
            invalid = (
                tokens(access="line\nbreak"),
                tokens(scopes=("dm.read",)),
                tokens(expires_in=29),
            )
            for value in invalid:
                with (
                    self.subTest(value=value),
                    self.assertRaises(integration_store.OAuthIntegrationValidationError),
                ):
                    store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, value, ACCOUNT)
                self.assertEqual(store.state_path.read_bytes(), original)

            store.state_path.chmod(0o644)
            with self.assertRaises(integration_store.OAuthIntegrationStoreError):
                store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state").mkdir(mode=0o700)
            target = root / "target.json"
            target.write_text('{"schema":1,"teams":{},"teams":{}}', encoding="utf-8")
            target.chmod(0o600)
            symlink = root / "state" / "integrations.json"
            symlink.symlink_to(target)
            with self.assertRaises(integration_store.OAuthIntegrationStoreError):
                self._store(root).metadata("team_1", "shimpz-cloudflare", DECLARATIONS)
            symlink.unlink()
            target.replace(symlink)
            with self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "duplicate"):
                self._store(root).metadata("team_1", "shimpz-cloudflare", DECLARATIONS)

    def test_retention_and_deletion_are_exactly_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            for team, assistant in (
                ("team_1", "first-assistant"),
                ("team_1", "second-assistant"),
                ("team_2", "second-assistant"),
            ):
                store.put(team, assistant, "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)

            self.assertFalse(store.retain_declared("team_1", "first-assistant", {"cloudflare": object()}))
            self.assertTrue(store.retain_declared("team_1", "first-assistant", {}))
            self.assertFalse(store.delete_integration("team_1", "first-assistant", "cloudflare"))
            self.assertEqual(
                store.metadata("team_1", "second-assistant", DECLARATIONS)[0].status,
                "connected",
            )
            self.assertTrue(store.delete_team("team_1"))
            self.assertEqual(
                store.metadata("team_1", "second-assistant", DECLARATIONS)[0].status,
                "missing",
            )
            self.assertTrue(store.delete_assistant("team_2", "second-assistant"))
            self.assertFalse(store.delete_assistant("team_2", "second-assistant"))
            self.assertFalse(store.delete_all())

    def test_revocation_transaction_keeps_authenticated_custody_until_callback_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.put("team_1", "shimpz-cloudflare", "cloudflare", "cloudflare", SCOPES, tokens(), ACCOUNT)
            observed: list[tuple[str, str, str | None, str | None]] = []

            def fail(provider: str, access: str, refresh: str | None, lease: str | None) -> None:
                observed.append((provider, access, refresh, lease))
                raise RuntimeError("synthetic upstream failure")

            with self.assertRaisesRegex(RuntimeError, "upstream failure"):
                store.revoke_then_delete("team_1", "shimpz-cloudflare", "cloudflare", fail)
            self.assertEqual(observed, [("cloudflare", ACCESS, REFRESH, None)])
            self.assertEqual(
                store.metadata("team_1", "shimpz-cloudflare", DECLARATIONS)[0].status,
                "connected",
            )

            self.assertTrue(
                store.revoke_then_delete(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    lambda provider, access, refresh, lease: observed.append((provider, access, refresh, lease)),
                )
            )
            self.assertEqual(
                observed,
                [("cloudflare", ACCESS, REFRESH, None), ("cloudflare", ACCESS, REFRESH, None)],
            )
            self.assertFalse(
                store.revoke_then_delete(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    lambda *_tokens: self.fail("missing integration must not invoke revocation"),
                )
            )

    def test_validation_helpers_reject_malformed_public_and_private_values(self) -> None:
        for function, arguments in (
            (integration_store._component_id, ("Bad", "component")),
            (integration_store._team_id, ("../team",)),
        ):
            with (
                self.subTest(function=function.__name__),
                self.assertRaises(integration_store.OAuthIntegrationValidationError),
            ):
                function(*arguments)

        self.assertIsNone(integration_store._bounded_text(None, "optional", 1, optional=True))
        with self.assertRaises(integration_store.OAuthIntegrationValidationError):
            integration_store._bounded_text("é", "bounded", 1)
        with self.assertRaises(integration_store.OAuthIntegrationValidationError):
            integration_store._integration(object())
        identity = integration_store.OAuthIntegrationIdentity("identity")
        self.assertEqual(integration_store._integration(identity), identity)
        with self.assertRaises(integration_store.OAuthIntegrationValidationError):
            integration_store._stored_status("invalid")
        with self.assertRaises(integration_store.OAuthIntegrationValidationError):
            integration_store._intent("missing", ())
        with self.assertRaises(integration_store.OAuthIntegrationValidationError):
            integration_store._token_set(object(), SCOPES, 1_000, None)
        with self.assertRaisesRegex(integration_store.OAuthIntegrationValidationError, "expiry"):
            integration_store._token_set(tokens(expires_in=30), SCOPES, 2**53 - 10, None)

        for payload, message in ((b"\xff", "valid JSON"), (b"{", "valid JSON")):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(
                    integration_store.OAuthIntegrationStoreError,
                    message,
                ),
            ):
                integration_store._strict_json(payload)

    def test_state_shape_helpers_reject_every_untrusted_boundary(self) -> None:
        malformed_metadata = {
            "provider": "missing",
            "scopes": [],
            "expires_at": 1,
            "status": "connected",
            "generation": 1,
        }
        with self.assertRaises(integration_store.OAuthIntegrationStoreError):
            integration_store._record_metadata(malformed_metadata)
        malformed_metadata.update(provider="cloudflare", scopes=list(SCOPES), expires_at=0)
        with self.assertRaises(integration_store.OAuthIntegrationStoreError):
            integration_store._record_metadata(malformed_metadata)

        for value in (
            {},
            {
                **malformed_metadata,
                "expires_at": 1,
                "updated_at": "invalid",
                "envelope": {},
            },
        ):
            with self.subTest(value=value), self.assertRaises(integration_store.OAuthIntegrationStoreError):
                integration_store._validate_record(value)

        invalid_states = (
            {},
            {"schema": 1, "teams": []},
            {"schema": 1, "teams": {"../team": {}}},
            {"schema": 1, "teams": {"team_1": []}},
            {"schema": 1, "teams": {"team_1": {"Bad": {}}}},
            {"schema": 1, "teams": {"team_1": {"assistant": []}}},
            {"schema": 1, "teams": {"team_1": {"assistant": {"Bad": {}}}}},
        )
        for state in invalid_states:
            with self.subTest(state=state), self.assertRaises(integration_store.OAuthIntegrationStoreError):
                integration_store._validate_state(state)

        with self.assertRaises(integration_store.OAuthIntegrationValidationError):
            integration_store._declarations([])
        with self.assertRaises(integration_store.OAuthIntegrationValidationError):
            integration_store._declarations({"integration": object()})
        for identifiers in (
            "integration",
            ("integration", "integration"),
            tuple(f"integration-{index}" for index in range(integration_store.MAX_INTEGRATIONS_PER_ASSISTANT + 1)),
        ):
            with (
                self.subTest(identifiers=identifiers),
                self.assertRaises(integration_store.OAuthIntegrationValidationError),
            ):
                integration_store._declared_ids(identifiers)

    def test_store_initialization_cache_clock_and_size_guards_fail_closed(self) -> None:
        with self.assertRaises(integration_store.OAuthIntegrationStoreError):
            integration_store.OAuthIntegrationStore(Path("state"), Path("key"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "separate"):
                integration_store.OAuthIntegrationStore(root / "state", root / "key")
            with self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "clock"):
                integration_store.OAuthIntegrationStore(root / "state" / "data", root / "key" / "key", clock=None)
            with (
                mock.patch.object(Path, "resolve", side_effect=OSError("offline")),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "unavailable"),
            ):
                integration_store.OAuthIntegrationStore(root / "state" / "data", root / "key" / "key")

            for clock in (lambda: True, lambda: -1, lambda: object()):
                store = self._store(root, clock=clock)
                with (
                    self.subTest(clock=clock),
                    self.assertRaisesRegex(
                        integration_store.OAuthIntegrationStoreError,
                        "clock",
                    ),
                ):
                    store._now()

            store = self._store(root)
            snapshot = SimpleNamespace(unchanged=True, payload=None, identity=None)
            with (
                mock.patch.object(
                    integration_store.private_state.PrivateState,
                    "read_private_file_if_changed",
                    return_value=snapshot,
                ),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "cache"),
            ):
                store._read_state()
            with (
                mock.patch.object(integration_store, "MAX_STATE_BYTES", 1),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "byte limit"),
            ):
                store._write_state(integration_store.private_state.empty_state())

    def test_plaintext_and_decrypted_envelopes_enforce_exact_shape_and_bounds(self) -> None:
        grant = integration_store._TokenGrant(ACCESS, REFRESH, None, SCOPES, 2_000, None, "connected")
        with (
            mock.patch.object(integration_store, "MAX_PLAINTEXT_BYTES", 1),
            self.assertRaisesRegex(integration_store.OAuthIntegrationValidationError, "too large"),
        ):
            integration_store.OAuthIntegrationStore._plaintext(grant)
        with (
            mock.patch.object(integration_store, "MAX_PLAINTEXT_BYTES", 1),
            self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "malformed"),
        ):
            integration_store.OAuthIntegrationStore._decrypted(
                b"{}",
                "cloudflare",
                SCOPES,
                2_000,
                "connected",
                1,
            )
        for payload in (b"{}", b'{"access_token":"","refresh_token":null,"broker_lease":null,"integration":null}'):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(
                    integration_store.OAuthIntegrationStoreError,
                    "malformed",
                ),
            ):
                integration_store.OAuthIntegrationStore._decrypted(
                    payload,
                    "cloudflare",
                    SCOPES,
                    2_000,
                    "connected",
                    1,
                )

    def test_record_capacity_total_limit_and_callback_contracts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            with (
                mock.patch.object(integration_store, "MAX_INTEGRATIONS_PER_ASSISTANT", 0),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "capacity"),
            ):
                store.put("team_1", "assistant", "integration", "cloudflare", SCOPES, tokens())

            store = self._store(root)
            store.put("team_1", "assistant", "integration", "cloudflare", SCOPES, tokens())
            state = json.loads(store.state_path.read_text(encoding="utf-8"))
            with (
                mock.patch.object(integration_store, "MAX_TOTAL_RECORDS", 0),
                self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "record limit"),
            ):
                integration_store._validate_state(state)

            with self.assertRaisesRegex(integration_store.OAuthIntegrationValidationError, "refresh callback"):
                store.resolve("team_1", "assistant", "integration", "cloudflare", SCOPES, None)
            with self.assertRaisesRegex(integration_store.OAuthIntegrationValidationError, "revocation callback"):
                store.revoke_then_delete("team_1", "assistant", "integration", None)

    def test_refresh_and_revocation_detect_concurrent_generation_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1_000]
            store = self._store(root, clock=lambda: now[0])
            store.put(
                "team_1",
                "assistant",
                "integration",
                "cloudflare",
                SCOPES,
                tokens(expires_in=30),
            )
            now[0] = 1_031

            def replace_during_refresh(_token: str, _lease: str | None) -> OAuthTokenSet:
                store.put(
                    "team_1",
                    "assistant",
                    "integration",
                    "cloudflare",
                    SCOPES,
                    tokens(access="concurrent-access-token-private-material"),
                )
                return tokens(access="refreshed-access-token-private-material")

            with self.assertRaisesRegex(
                integration_store.OAuthIntegrationReauthorizationError,
                "changed during refresh",
            ):
                store.resolve(
                    "team_1",
                    "assistant",
                    "integration",
                    "cloudflare",
                    SCOPES,
                    replace_during_refresh,
                )

            def delete_during_revocation(*_tokens: object) -> None:
                store.delete_integration("team_1", "assistant", "integration")

            with self.assertRaisesRegex(integration_store.OAuthIntegrationStoreError, "changed during revocation"):
                store.revoke_then_delete(
                    "team_1",
                    "assistant",
                    "integration",
                    delete_during_revocation,
                )

    def test_delete_team_and_all_report_both_state_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            self.assertFalse(store.delete_team("team_1"))
            store.put("team_1", "assistant", "integration", "cloudflare", SCOPES, tokens())
            self.assertTrue(store.delete_all())
            self.assertFalse(store.delete_all())

    def test_declared_grant_distinguishes_missing_and_revoked_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            with self.assertRaises(integration_store.OAuthIntegrationMissingError):
                store._declared_grant("team_1", "assistant", "integration", "cloudflare", SCOPES)

            store.put("team_1", "assistant", "integration", "cloudflare", SCOPES, tokens())
            revoked = integration_store._TokenGrant(
                ACCESS,
                REFRESH,
                None,
                SCOPES,
                2_000,
                None,
                "reauthorization-required",
                1,
            )
            with (
                mock.patch.object(store, "_resolve_record", return_value=revoked),
                self.assertRaises(integration_store.OAuthIntegrationReauthorizationError),
            ):
                store._declared_grant("team_1", "assistant", "integration", "cloudflare", SCOPES)


if __name__ == "__main__":
    unittest.main()
