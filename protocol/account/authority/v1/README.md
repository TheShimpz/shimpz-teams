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

Every response, including a denial, is a JSON object with an explicit body
length from 1 through 8192 bytes. Statuses 401, 403, and 404 deny the requested
authority; 404 is reserved for an unavailable target Owner. Statuses 400, 429,
and 5xx mean that the consumer could not obtain valid authority evidence.

Ordinary Accounts create self-owned Teams. A request may include
`owner_account_id` only for `team-create`; Account accepts it only from a
Supervisor and only when the exact target Account is enabled and not erased.

Every object is closed. Tokens and protected request contents must never appear
in logs, errors, or evidence responses.
