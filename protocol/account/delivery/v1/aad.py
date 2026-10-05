"""Account Integration-secret delivery protocol v1: the exact encryption context both ends bind.

Account owns this module; Team consumes an exact, commit-pinned byte mirror. Integration Secrets uses the returned
bytes as both the HKDF-SHA256 info and the AES-256-GCM associated data when it seals one delivery to a Team's one-use
X25519 key, and Team rebuilds the same bytes to open it. Metadata admission and error mapping stay with each consumer.
"""

from __future__ import annotations

import base64
import json

VERSION = 1
ALGORITHM = "X25519-HKDF-SHA256+A256GCM"
PURPOSE = "shimpz-integration-secret-delivery"
PUBLIC_KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32


def encode(value: bytes) -> str:
    """Encode delivery bytes as padded URL-safe base64, the form every delivery field uses on the wire."""
    return base64.urlsafe_b64encode(value).decode()


def delivery_aad(
    account_id: str,
    provider: str,
    auth_type: str,
    recipient_public_key: bytes,
    sender_public_key: bytes,
) -> bytes:
    """Return the canonical JSON encryption context that binds one delivery to its Account, provider, and keys."""
    return json.dumps(
        {
            "account_id": account_id,
            "alg": ALGORITHM,
            "auth_type": auth_type,
            "provider": provider,
            "purpose": PURPOSE,
            "recipient_public_key": encode(recipient_public_key),
            "sender_public_key": encode(sender_public_key),
            "v": VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
