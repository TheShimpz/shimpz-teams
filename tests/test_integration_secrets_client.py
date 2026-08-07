from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from inference import integration_secrets as integration_secrets_client


def _delivery(account_id: str, provider: str, recipient: str, secret: str) -> dict[str, object]:
    recipient_bytes = integration_secrets_client._b64decode(recipient)
    sender = x25519.X25519PrivateKey.generate()
    sender_public = sender.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    salt = secrets.token_bytes(integration_secrets_client.DELIVERY_SALT_BYTES)
    nonce = secrets.token_bytes(integration_secrets_client.DELIVERY_NONCE_BYTES)
    aad = integration_secrets_client._delivery_aad(
        account_id,
        provider,
        "api_key",
        recipient_bytes,
        sender_public,
    )
    shared_key = sender.exchange(x25519.X25519PublicKey.from_public_bytes(recipient_bytes))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=integration_secrets_client.DELIVERY_KEY_BYTES,
        salt=salt,
        info=aad,
    ).derive(shared_key)
    ciphertext = AESGCM(key).encrypt(nonce, secret.encode(), aad)
    return {
        "v": integration_secrets_client.DELIVERY_VERSION,
        "alg": integration_secrets_client.DELIVERY_ALGORITHM,
        "sender_public_key": integration_secrets_client._b64encode(sender_public),
        "salt": integration_secrets_client._b64encode(salt),
        "nonce": integration_secrets_client._b64encode(nonce),
        "ciphertext": integration_secrets_client._b64encode(ciphertext),
    }


class IntegrationSecretsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        integration_secrets_client._token_cache.clear()

    def test_resolve_delivers_only_an_api_key_in_memory(self):
        account_id = "account-1"
        provider = "openai"
        secret = secrets.token_urlsafe(32)
        requests: list[tuple[str, dict]] = []

        def post(_base_url, path, payload, _token_file, _session=None):
            requests.append((path, payload))
            if path == "/v1/internal/model-providers/resolve":
                return 200, {
                    "auth_type": "api_key",
                    "secret_ref": {"opaque": "envelope"},
                    "generation": 4,
                }
            self.assertEqual(path, "/v1/deliver")
            return 200, {
                "delivery": _delivery(
                    account_id,
                    provider,
                    payload["recipient_public_key"],
                    secret,
                )
            }

        with mock.patch.object(integration_secrets_client, "_post", side_effect=post):
            credential = integration_secrets_client.resolve(account_id, provider)

        self.assertEqual(credential, ("api_key", secret, 4))
        self.assertEqual(
            [path for path, _payload in requests],
            [
                "/v1/internal/model-providers/resolve",
                "/v1/deliver",
            ],
        )
        self.assertNotIn(secret, json.dumps(requests))

    def test_unsupported_providers_and_oauth_metadata_fail_closed(self):
        for provider in ("claude-code", "codex"):
            with self.subTest(provider=provider), mock.patch.object(integration_secrets_client, "_post") as post:
                with self.assertRaises(integration_secrets_client.IntegrationSecretError):
                    integration_secrets_client.resolve("account-1", provider)
                post.assert_not_called()

        metadata = {
            "auth_type": "oauth",
            "secret_ref": {"opaque": "invalid-envelope"},
            "generation": 1,
        }
        with mock.patch.object(integration_secrets_client, "_post", return_value=(200, metadata)) as post:
            with self.assertRaises(integration_secrets_client.IntegrationSecretError):
                integration_secrets_client.resolve("account-1", "anthropic")
            post.assert_called_once()

    def test_generation_check_keeps_revocation_authority_in_account(self):
        with mock.patch.object(
            integration_secrets_client,
            "_post",
            side_effect=((200, {"valid": True}), (409, {"valid": False})),
        ):
            self.assertTrue(integration_secrets_client.generation_is_current("account-1", "openai", 3))
            self.assertFalse(integration_secrets_client.generation_is_current("account-1", "openai", 3))

        with mock.patch.object(integration_secrets_client, "_post") as post:
            with self.assertRaises(integration_secrets_client.IntegrationSecretError):
                integration_secrets_client.generation_is_current("account-1", "codex", 3)
            post.assert_not_called()

    def test_session_reuses_transport_without_reusing_authorization_results(self):
        class Response:
            status = 200

            @staticmethod
            def read(_maximum):
                return b'{"valid":true}'

        class Connection:
            def __init__(self) -> None:
                self.requests = 0
                self.closes = 0

            def request(self, *_args) -> None:
                self.requests += 1

            @staticmethod
            def getresponse():
                return Response()

            def close(self) -> None:
                self.closes += 1

        connection = Connection()
        constructors = mock.Mock(return_value=connection)
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("service-token", encoding="utf-8")
            with (
                mock.patch.object(integration_secrets_client.http.client, "HTTPConnection", constructors),
                integration_secrets_client.IntegrationSecretSession() as session,
            ):
                first = integration_secrets_client._post(
                    "http://account:7079",
                    "/v1/internal/model-providers/generation-check",
                    {"generation": 1},
                    token_path,
                    session,
                )
                second = integration_secrets_client._post(
                    "http://account:7079",
                    "/v1/internal/model-providers/generation-check",
                    {"generation": 1},
                    token_path,
                    session,
                )

        self.assertEqual(first, (200, {"valid": True}))
        self.assertEqual(second, first)
        constructors.assert_called_once_with("account", 7079, timeout=10)
        self.assertEqual(connection.requests, 2)
        self.assertEqual(connection.closes, 1)

    def test_session_retries_once_when_an_idle_connection_was_closed(self):
        class Response:
            status = 200
            will_close = False

            @staticmethod
            def read(_maximum):
                return b'{"valid":true}'

        class Connection:
            def __init__(self, *, fail_on_request: int | None = None) -> None:
                self.fail_on_request = fail_on_request
                self.requests = 0
                self.closes = 0

            def request(self, *_args) -> None:
                self.requests += 1
                if self.requests == self.fail_on_request:
                    raise integration_secrets_client.http.client.RemoteDisconnected("idle close")

            @staticmethod
            def getresponse():
                return Response()

            def close(self) -> None:
                self.closes += 1

        stale = Connection(fail_on_request=2)
        replacement = Connection()
        constructors = mock.Mock(side_effect=(stale, replacement))
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("service-token", encoding="utf-8")
            with (
                mock.patch.object(integration_secrets_client.http.client, "HTTPConnection", constructors),
                integration_secrets_client.IntegrationSecretSession() as session,
            ):
                first = integration_secrets_client._post(
                    "http://account:7079",
                    "/v1/internal/model-providers/generation-check",
                    {"generation": 1},
                    token_path,
                    session,
                )
                second = integration_secrets_client._post(
                    "http://account:7079",
                    "/v1/internal/model-providers/generation-check",
                    {"generation": 2},
                    token_path,
                    session,
                )

        self.assertEqual(first, (200, {"valid": True}))
        self.assertEqual(second, first)
        self.assertEqual(constructors.call_count, 2)
        self.assertEqual(stale.requests, 2)
        self.assertEqual(stale.closes, 1)
        self.assertEqual(replacement.requests, 1)
        self.assertEqual(replacement.closes, 1)

    def test_token_cache_reopens_metadata_and_rereads_only_after_replacement(self):
        integration_secrets_client._token_cache.clear()
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("first-token", encoding="utf-8")
            original_read = integration_secrets_client.os.read
            with mock.patch.object(integration_secrets_client.os, "read", wraps=original_read) as read:
                self.assertEqual(integration_secrets_client._token(token_path), "first-token")
                self.assertEqual(integration_secrets_client._token(token_path), "first-token")
                first_read_count = read.call_count
                replacement = token_path.with_name("replacement")
                replacement.write_text("second-token", encoding="utf-8")
                replacement.replace(token_path)
                self.assertEqual(integration_secrets_client._token(token_path), "second-token")

        self.assertEqual(first_read_count, 1)
        self.assertEqual(read.call_count, 2)

    def test_volume_archive_helpers_are_not_part_of_the_runtime_contract(self):
        for absent_name in ("credential_file", "credential_archive", "resolve_archive"):
            with self.subTest(name=absent_name):
                self.assertFalse(hasattr(integration_secrets_client, absent_name))

    def test_ciphertext_fields_require_canonical_base64_strings(self) -> None:
        for value in (None, "!"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    integration_secrets_client.IntegrationSecretError,
                    "invalid ciphertext",
                ),
            ):
                integration_secrets_client._b64decode(value)

    def test_delivery_envelope_shape_lengths_and_authentication_fail_closed(self) -> None:
        private_key = x25519.X25519PrivateKey.generate()
        recipient = integration_secrets_client._b64encode(
            private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        valid = _delivery("account-1", "openai", recipient, "secret")
        invalid = (
            None,
            {**valid, "v": 2},
            {**valid, "alg": "unknown"},
            {**valid, "sender_public_key": integration_secrets_client._b64encode(b"short")},
            {**valid, "ciphertext": integration_secrets_client._b64encode(b"x" * 16)},
            {**valid, "ciphertext": integration_secrets_client._b64encode(b"x" * 17)},
        )
        for delivery in invalid:
            with self.subTest(delivery=delivery), self.assertRaises(integration_secrets_client.IntegrationSecretError):
                integration_secrets_client._open_delivery(
                    private_key,
                    "account-1",
                    "openai",
                    "api_key",
                    delivery,
                )

        tampered = dict(valid)
        tampered["ciphertext"] = integration_secrets_client._b64encode(
            integration_secrets_client._b64decode(valid["ciphertext"])[:-1] + b"x"
        )
        with self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "authentication failed"):
            integration_secrets_client._open_delivery(
                private_key,
                "account-1",
                "openai",
                "api_key",
                tampered,
            )

    def test_delivery_rejects_nul_plaintext(self) -> None:
        private_key = x25519.X25519PrivateKey.generate()
        recipient = integration_secrets_client._b64encode(
            private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        delivery = _delivery("account-1", "openai", recipient, "bad\0secret")
        with self.assertRaisesRegex(
            integration_secrets_client.IntegrationSecretError,
            "invalid plaintext",
        ):
            integration_secrets_client._open_delivery(
                private_key,
                "account-1",
                "openai",
                "api_key",
                delivery,
            )

    @staticmethod
    def _token_metadata(size: int, *, inode: int = 1, mode: int = 0o100600) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=mode,
            st_nlink=1,
            st_size=size,
            st_dev=1,
            st_ino=inode,
            st_mtime_ns=1,
            st_ctime_ns=1,
        )

    def test_token_reader_rejects_unsafe_unavailable_and_changed_files(self) -> None:
        path = Path("/token")
        unsafe = self._token_metadata(5, mode=0o040700)
        with (
            mock.patch.object(integration_secrets_client.os, "open", return_value=3),
            mock.patch.object(integration_secrets_client.os, "fstat", return_value=unsafe),
            mock.patch.object(integration_secrets_client.os, "close"),
            self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "unavailable"),
        ):
            integration_secrets_client._token(path)

        with (
            mock.patch.object(integration_secrets_client.os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "unavailable"),
        ):
            integration_secrets_client._token(path)

        before = self._token_metadata(5)
        after = self._token_metadata(5, inode=2)
        with (
            mock.patch.object(integration_secrets_client.os, "open", return_value=3),
            mock.patch.object(integration_secrets_client.os, "fstat", side_effect=(before, after)),
            mock.patch.object(integration_secrets_client.os, "read", return_value=b"token"),
            mock.patch.object(integration_secrets_client.os, "close"),
            self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "unavailable"),
        ):
            integration_secrets_client._token(path)

        with (
            mock.patch.object(integration_secrets_client.os, "open", return_value=3),
            mock.patch.object(integration_secrets_client.os, "fstat", return_value=before),
            mock.patch.object(integration_secrets_client.os, "read", return_value=b""),
            mock.patch.object(integration_secrets_client.os, "close"),
            self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "unavailable"),
        ):
            integration_secrets_client._token(path)

    def test_token_reader_rejects_non_utf8_and_control_bearing_tokens(self) -> None:
        path = Path("/token")
        for raw in (b"\xff", b"\n", b"bad\ntoken"):
            metadata = self._token_metadata(len(raw))
            with (
                self.subTest(raw=raw),
                mock.patch.object(integration_secrets_client.os, "open", return_value=3),
                mock.patch.object(integration_secrets_client.os, "fstat", return_value=metadata),
                mock.patch.object(integration_secrets_client.os, "read", return_value=raw),
                mock.patch.object(integration_secrets_client.os, "close"),
                self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "unavailable"),
            ):
                integration_secrets_client._token(path)

    def test_token_reader_preserves_the_unopened_descriptor_sentinel(self) -> None:
        path = Path("/token")
        metadata = self._token_metadata(5)
        with (
            mock.patch.object(integration_secrets_client.os, "open", return_value=-1),
            mock.patch.object(integration_secrets_client.os, "fstat", return_value=metadata),
            mock.patch.object(integration_secrets_client.os, "read", return_value=b"token"),
            mock.patch.object(integration_secrets_client.os, "close") as close,
        ):
            self.assertEqual(integration_secrets_client._token(path), "token")
        close.assert_not_called()

    def test_session_discard_without_a_connection_is_idempotent(self) -> None:
        integration_secrets_client.IntegrationSecretSession().discard((object(), "host", 80))

    def test_post_rejects_invalid_urls_transport_and_response_shapes(self) -> None:
        with self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "unavailable"):
            integration_secrets_client._post("file:///secret", "/v1/test", {}, Path("/token"))

        class Response:
            def __init__(self, raw: bytes, *, will_close: bool = False) -> None:
                self.status = 200
                self.raw = raw
                self.will_close = will_close

            def read(self, _maximum: int) -> bytes:
                return self.raw

        class Connection:
            def __init__(self, response: Response, *, error: BaseException | None = None) -> None:
                self.response = response
                self.error = error
                self.closed = 0

            def request(self, *_args) -> None:
                if self.error is not None:
                    raise self.error

            def getresponse(self) -> Response:
                return self.response

            def close(self) -> None:
                self.closed += 1

        cases = (
            (Connection(Response(b"{}"), error=OSError("offline")), "unavailable"),
            (
                Connection(Response(b"x" * (integration_secrets_client.MAX_RESPONSE_BYTES + 1))),
                "invalid response",
            ),
            (Connection(Response(b"{")), "invalid response"),
            (Connection(Response(b"[]", will_close=True)), "invalid response"),
        )
        for connection, message in cases:
            with (
                self.subTest(message=message),
                mock.patch.object(integration_secrets_client, "_token", return_value="token"),
                mock.patch.object(
                    integration_secrets_client.http.client,
                    "HTTPConnection",
                    return_value=connection,
                ),
                self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, message),
            ):
                integration_secrets_client._post("http://service:80", "/v1/test", {}, Path("/token"))
            self.assertGreaterEqual(connection.closed, 1)

    def test_resolution_and_generation_statuses_are_closed(self) -> None:
        with mock.patch.object(integration_secrets_client, "_post", return_value=(404, {})):
            self.assertIsNone(integration_secrets_client.resolve("account-1", "openai"))
        with (
            mock.patch.object(integration_secrets_client, "_post", return_value=(503, {})),
            self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "lookup failed"),
        ):
            integration_secrets_client.resolve("account-1", "openai")

        metadata = {"auth_type": "api_key", "secret_ref": {}, "generation": 1}
        with (
            mock.patch.object(
                integration_secrets_client,
                "_post",
                side_effect=((200, metadata), (503, {"secret": "must-not-exist"})),
            ),
            self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "delivery failed"),
        ):
            integration_secrets_client.resolve("account-1", "openai")

        for generation in (True, 0, "1"):
            with (
                self.subTest(generation=generation),
                self.assertRaisesRegex(
                    integration_secrets_client.IntegrationSecretError,
                    "generation is invalid",
                ),
            ):
                integration_secrets_client.generation_is_current("account-1", "openai", generation)
        with (
            mock.patch.object(integration_secrets_client, "_post", return_value=(200, {"valid": False})),
            self.assertRaisesRegex(integration_secrets_client.IntegrationSecretError, "generation check failed"),
        ):
            integration_secrets_client.generation_is_current("account-1", "openai", 1)


if __name__ == "__main__":
    unittest.main()
