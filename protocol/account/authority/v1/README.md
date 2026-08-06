# Account authority protocol v1

Account owns this protocol. Hosted Team uses it to turn one current Account
session into request-bound human authority; no consumer may infer human
authority from a machine bearer.

`POST /v1/internal/authority/evaluate` requires the dedicated Team capability.
The request binds the resolved HTTP method, canonical operation, validated
route parameters, duplicate-free query, and a body descriptor. Consumers must
send every query key and value exactly once and deny the request if either
cannot be represented; they must never omit, normalize, or truncate one:

- `none` binds the zero length and the SHA-256 digest of empty bytes;
- `json` binds the exact raw byte length and SHA-256 digest before parsing;
- `file` binds the validated length, decoded filename of at most 255 UTF-8
  bytes, and media type before reading at most 25 MiB of content.

The Account session is intentionally outside `binding_digest`: it selects the
human principal, while the digest identifies the exact request that the Team
must compare. Account returns no wall-clock validity window or security epoch.
Team accepts the evidence only on the same synchronous connection and enforces
a short monotonic round trip.

A Hosted `chat-human-submit` binding may additionally carry the exact
`assurance` class and Power challenge ID. Only that operation accepts it, and
its parameters must include the exact Team ID. The matching request carries a
43-character opaque `assurance_handle` produced by one fresh Account password,
TOTP, or UV-required WebAuthn ceremony. Account consumes the memory-only handle
once and returns only the same `{kind, challenge_id}` evidence. Handles are
bound to the exact active Account session, Team, challenge, and assurance kind,
expire after a fixed 300 seconds, and disappear on Account restart. Recovery
codes never produce Power assurance. Factor material and the opaque handle are
never returned to Team as authority evidence or written to an audit.

Every response, including a denial, is a JSON object with an explicit body
length from 1 through 8192 bytes. Statuses 401, 403, and 404 deny the requested
authority; 404 is reserved for an unavailable target Owner. Statuses 400, 429,
and 5xx mean that the consumer could not obtain valid authority evidence.

Ordinary Accounts create self-owned Teams. A request may include
`owner_account_id` only for `team-create`; Account accepts it only from a
Supervisor and only when the exact target Account is enabled and not erased.

Every object is closed. Tokens and protected request contents must never appear
in logs, errors, or evidence responses.
