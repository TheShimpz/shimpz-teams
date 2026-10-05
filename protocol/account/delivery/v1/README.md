# Account Integration-secret delivery protocol v1

Account owns this protocol. Integration Secrets uses it to re-encrypt one sealed model-provider credential to a
Team's one-use X25519 public key, and Hosted Team uses it to open that delivery in memory. Team consumes an exact,
commit-pinned byte mirror of this directory and never edits it.

A delivery is a JSON object with exactly `v` (`1`), `alg` (`X25519-HKDF-SHA256+A256GCM`), `sender_public_key`
(32 bytes), `salt` (16 bytes), `nonce` (12 bytes), and `ciphertext`. Every byte field is padded URL-safe base64.
The recipient derives the 32-byte AES-256-GCM key with HKDF-SHA256 over the X25519 shared secret and the salt.

`aad.py` produces the encryption context: the UTF-8 bytes of the JSON object with the keys `account_id`, `alg`,
`auth_type`, `provider`, `purpose` (`shimpz-integration-secret-delivery`), `recipient_public_key`,
`sender_public_key`, and `v`, serialized with sorted keys, `,` and `:` separators, and ASCII escaping. Both public
keys appear as padded URL-safe base64 of their 32 raw bytes. The same bytes are the HKDF `info` and the AES-GCM
associated data, so a delivery opens only for the exact Account, provider, auth type, and key pair it was sealed to.

Each consumer validates its own metadata and maps its own errors before calling `delivery_aad`; this protocol never
admits an Account, provider, or credential. `vectors.json` fixes the exact bytes for known inputs, and
`contract-files.sha256` pins every contract file. The at-rest envelope context stays private to Integration
Secrets and is not part of this protocol.
