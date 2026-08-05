# Team HTTP protocol v1

Team owns the closed identifiers, payload projections, and WebSocket frame boundary used by Admin
and Store. `payload.py` validates Team-facing HTTP values without trusting upstream fields.
`websocket.py` validates the bounded `shimpz.chat.v3` frame primitives and redacts unsafe errors.
`progress.py` owns the closed metadata-only progress events and NDJSON terminal framing used by
Local Team chat. A Power occurrence carries only its canonical reviewed Assistant and Power
identifiers; it never carries arguments, results, prompts, model output, or free text. Progress is
advisory; only the single terminal record determines the operation outcome. A missing, repeated,
malformed, oversized, or out-of-order record fails closed at the consumer without widening Team
authority or exposing execution payloads.
Thread pools, queues, worker limits, and saturation behavior are deployable-owned runtime policy,
not part of this wire protocol.

An authenticated Supervisor may read the canonical PNG for one installed Assistant from
`GET /v1/teams/:team_id/assistants/:assistant_id/icon`. Team resolves the current durable binding,
verifies the icon digest again at read time, returns exactly `image/png`, and marks the response
`no-store`. Missing bindings fail as absent; missing or tampered custody fails closed.

In the Hosted profile, every human Team operation carries exactly one `X-Shimpz-Account`
header containing the current opaque Account session. Team binds the canonical route, parameters,
query, and exact request-body evidence before synchronously asking Account to evaluate that session.
The internal Team bearer is machine authority only for the one-use OAuth callback continuation; it
never substitutes for human Account or Supervisor evidence. In Local, Admin emits one short-lived
Ed25519 assertion in `X-Shimpz-Supervisor` after validating its current browser session; Team binds
it to the canonical request and consumes it once while retaining an independent machine bearer.

`vectors.json` contains positive and negative cases that Team, Admin, and Store execute
independently. Generated consumer mirrors pin the producing Teams commit, verify
`contract-files.sha256`, and remain byte-identical to this directory.

Validate the authority from this directory:

```console
python verify.py
```
