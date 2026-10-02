# Shimpz Assistant-install contract v1

This directory is the language-neutral authority for the narrow boundary
between Developers and the Team.

It covers:

- EdDSA JWS delegation claims for Team listing and Assistant installation;
- the authorized Team list;
- immutable dynamic Assistant resolution;
- the hosted Controller install request and result; and
- the final, context-bound install authorization request and receipt.

Every authoritative object is closed. Unknown fields fail validation. The
schemas use JSON Schema draft 2020-12 and share definitions through
`definitions.schema.json`; each operation has a small standalone entry-point
schema.

## Security boundary

The JWT wire fields `iss` and `aud` retain their standard interoperable names.
Product code should expose them as `issuer` and `audience` and construct claims
through operation-specific helpers. Their values are fixed:

```text
issuer:   https://developers.shimpz.com
audience: https://developers.shimpz.com/assistant-install
```

A delegation is valid for at most 60 seconds. An install authorization is valid
for at most 120 seconds. JSON Schema validates their shape; the reference
verifier and every consumer also enforce these lifetime relationships.

The install JSON is never authority by itself. The internal request
must also carry the named Developers service credential and the compact
delegation JWS. The claims and body must bind the same account, Team, source
digest, request ID, and idempotency key before Team invokes its
existing ownership authorization.

Resolve returns only an unblocked, installable publication. It contains a full
digest image reference, the published Assistant name and summary, the SHA-256
digest of the canonical `icon.png`, and the complete `assistant-direct-v1`
envelope. The binary icon is fetched from
Developers by exact `source_digest` and accepted only when its digest matches;
it is never embedded in resolution JSON or supplied by an authored URL. Modes
are JSON integers: `365` is octal `0555`, and `292` is octal `0444`. No port,
health endpoint, authored server, mutable image, capability, or alternative
runtime setting is admitted.

The machine contract carries the Assistant's English message catalog as a sorted `messages` list of
`{id, msgid, max_length, params}` (Developers Assistant Spec v1 owns its complete semantics). Every `id` is the
lowercase SHA-256 of its `msgid` bytes, and the published `summary` is one catalog message with no parameters and a
`max_length` of at most 160.

Every machine-contract Action carries its `effect`, `read_only` or `mutating`, and a `mutating` Action may carry one
closed `verifier` descriptor naming a `read_only` Action of the same contract, its typed input bindings from the
original input or `operation_id`, and the RFC 6901 pointers of its outcome and recovered result. A `mutating` Action
may also carry one closed `idempotency` declaration: the provider host, the key location and name, the key scope,
the provider's retention in seconds, and whether a reused key requires the same payload. These schemas fix
the shape only; Developers Assistant Spec v1 owns the semantics, and Developers and Team each enforce them.

Language packs travel with the artifact. Each final image holds the canonical pack for that catalog at the fixed
read-only `/opt/shimpz/shimpz.pack.json` (`language_pack` in the runtime), and resolve carries its `pack_digest`:
`sha256:` over the exact pack bytes. The signed provenance binds the same digest beside the manifest and machine
contract digests. The Controller reads the pack from the verified image, requires that its bytes hash to
`pack_digest` and that it is complete and valid for the resolved catalog, and keeps it with the reviewed binding.
A missing, modified, incomplete, or mismatched pack fails closed.

Signature and DSSE provenance bundles are not embedded. Resolve supplies
immutable references under
`ghcr.io/theshimpz/shimpz-assistant-trust` and the signer identity. The
Controller fetches both trust artifacts and verifies them with Cosign against
the full executable image digest. Missing, unavailable, invalid, or
inconsistent trust material fails closed.

Each declared Action admits at most one authorization capability: `approval`,
`auth:password`, `auth:totp`, or `auth:passkey`. Input requests remain
independent. An authentication ceremony both proves the named mechanism and
authorizes the exact challenge, so it is never combined with `approval` for the
same Action.

Resolve also carries each reviewed Stored Input declaration and every Action's exact use list. Stored Inputs are
token-like third-party credentials rather than OAuth Integrations. The declaration contains only id, closed
`password` kind, label, description, and an optional `help_url`; no value, configured status, or ciphertext crosses
this boundary. `help_url` is the page where a person creates the value: at most 2,048 characters of one canonical
`https` URL on a public DNS host, with a path, an optional query, and no port, credentials, fragment, or dot
segment, written exactly as WHATWG URL serialization would print it. Every Action references at most one declared
id.

## Golden vectors

`vectors.json` contains reusable fixtures and positive and negative cases for
every entry point. A case may apply one deterministic mutation:

- `set` creates or replaces an object property;
- `remove` removes an existing object property; and
- `path` is a non-empty array of object-property names.

This small mutation format keeps security-sensitive resolve vectors readable
without making consumers implement a general patch language.

Validate the frozen authority and every vector from this directory:

```console
python verify.py
```

Synchronize the already-verified authority into an empty or previously
synchronized consumer directory:

```console
python verify.py --sync /path/to/consumer/protocol/install/v1
```

The sync rejects symlinks, special files, and unknown destination entries.
Consumers record the producing umbrella commit and verify
`contract-files.sha256` before running their independent implementation against
the unchanged vectors.
