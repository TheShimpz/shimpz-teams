# Team HTTP protocol v1

Team owns the closed identifiers, payload projections, and WebSocket frame boundary used by Admin
and Store. `payload.py` validates Team-facing HTTP values without trusting upstream fields.
`websocket.py` validates the bounded `shimpz.chat.v7` frame primitives and redacts unsafe errors.
`progress.py` owns the closed metadata-only progress events and NDJSON terminal framing used by
Local Team chat. An Action occurrence carries only its canonical reviewed Assistant and Action
identifiers; it never carries arguments, results, prompts, model output, or free text. Progress is
advisory; only the single terminal record determines the operation outcome. A missing, repeated,
malformed, oversized, or out-of-order record fails closed at the consumer without widening Team
authority or exposing execution payloads.
Thread pools, queues, worker limits, and saturation behavior are deployable-owned runtime policy,
not part of this wire protocol.

`shimpz.chat.v7` retains the Local Admin's exact `human-response` client frame. It binds a `submit` or
`deny` decision to one opaque lowercase 32-hex challenge. Submitted values admit only `true`, one
bounded string, or one bounded unique string list. The pending reviewed descriptor determines the
actual request kind and tighter bounds; the Team revalidates it authoritatively. For Local
`auth:password`, the browser submits the password only to Admin, Admin replaces it with `true` after
verification, and the signed Local assertion binds the successful assurance to the same challenge.
In Hosted, the browser completes the requested Account ceremony, receives one opaque Account-issued
handle, and submits it as `value` over the chat surface; Store relays it unmodified. Team only
pattern-admits and forwards that credential to Account, then replaces it with `true` before Action
resumption after Account consumes it successfully. Handle issuance, freshness, binding, one-use
semantics, and factor custody remain Account authority. Authentication factor material never crosses
to Team, Brain, an Assistant, or a progress event.

Local Admin may also emit the exact aggregate `assistant-install-plan` lifecycle for an authenticated
Supervisor task. A `planned` event carries one socket-scoped plan id and at most four sorted Assistants
with bounded public display identity, sorted Integration providers, and per-item `pending` status.
Subsequent `installing` events preserve that identity while advancing a single sequential item through
`installing` and `installed`; the terminal lifecycle state is `installed`, `failed`, or `stopped`. An `installed`
event requires `continuation` as exactly `dispatch` or `none`; the field is forbidden on every other state. When
every exact named Assistant was already confirmed running, that terminal event additionally carries only
`outcome: already-installed`, requires `continuation: none`, and represents current state rather than fresh work. A
failed event carries one bounded HTTP status and retains already-installed items without rollback. The browser
never sends the plan id, planned Assistant ids, or publication digest back to Admin. After every item is freshly
proved running, an exact install-only command terminates at the installed result with `continuation: none`; this
also applies when an exact composed request mixes current and freshly installed identities. Every other task uses
`continuation: dispatch` and runs exactly once with the admitted scope union.

Socket loss still drops the active objective and every unstarted plan item; no server or lifecycle state
persists or replays them. The Admin browser may retain one prior ordinary objective only in current-page
memory and consume it once when the same Team and exact Assistant scope submit a closed capability-continuation
message. That fresh authenticated request has this exact Admin-only shape:

```json
{"type":"resume-task","message":"Can you enable it?","objective":"List my DNS zones","files":[],"assistant_ids":[],"objective_assistant_ids":[]}
```

Both messages and both Assistant scopes satisfy the ordinary chat bounds, `files` is exactly empty, and the two
Assistant scopes are identical. `message` must be one complete member of Admin's closed continuation vocabulary;
`objective` must be neither a continuation nor an uninstall request. An active turn, pending human or Integration
challenge, or pending uninstall decision rejects the frame. Admin plans only `objective`: no capability gap sends
only `message` to Team; a completed installation terminates at `installed` when `objective` is an exact install-only
command and otherwise sends only `objective` with the admitted union scope; planner failure or Stop sends neither.
The browser visibly attributes the resumed objective, clears it on Team change, scope change, uninstall proposal,
page disposal, or consumption, and never writes it to browser storage. The retained Hosted Store backend does not
accept `resume-task`.

Local Admin may also emit the exact `assistant-uninstall` lifecycle. A terminal `target-required` event carries
only the authenticated socket Team id and instructs the browser to ask for an Assistant name; it never creates a
proposal, infers a target, queries inventory, or invokes Brain. Its `proposed` event carries only Team-derived
bounded display identity and the installed semantic version; later `uninstalling`, `uninstalled`, `cancelled`,
`expired`, or `failed` events correlate that proposal. The browser never sends the proposal id, Assistant id,
version, or a deletion target. Admin requires closed destructive intent, uses a removal-specific confirmation
vocabulary, and revalidates Team presence and version immediately before invoking the existing Team-owned
uninstall route. Store data and Store icon routes never participate.

An authenticated Supervisor may read the canonical PNG for one installed Assistant from
`GET /v1/teams/:team_id/assistants/:assistant_id/icon`. Team resolves the current durable binding,
verifies the icon digest again at read time, returns exactly `image/png`, and marks the response
`no-store`. Missing bindings fail as absent; missing or tampered custody fails closed.

Local Admin may request presentation-only labels for one installed binding from
`POST /v1/teams/:team_id/assistants/:assistant_id/action-labels`. The exact request body is
`{"language_exemplar":"..."}` and carries the same request-scoped model credential headers as chat.
Team supplies Brain only the bounded exemplar and the binding's canonical Action ids, then revalidates
the Team generation, Assistant version, Action-id set, provider, and model after the stateless model call.
The response contains `team_id`, `assistant`, `assistant_version`, and every exact Action as an `id` plus
an inert bounded `label`; the HTTP adapter adds `trace_id`. Labels never replace canonical ids, enter chat
history, describe Action schemas, or grant authority. Binding drift fails closed. Model or label failure is
availability failure after installation and must not be represented as installation rollback.

In the Hosted profile, every human Team operation carries exactly one `X-Shimpz-Account`
header containing the current opaque Account session. Team binds the canonical route, parameters,
query, and exact request-body evidence before synchronously asking Account to evaluate that session.
The internal Team bearer is machine authority only for the one-use OAuth callback continuation and
the Local bootstrap reset. The bootstrap reset is admitted only while Team independently verifies
that the Supervisor key directory is safe and the Supervisor public key is absent; after identity
establishment it fails closed and never substitutes for human Supervisor evidence. In Local, Admin emits one short-lived
Ed25519 assertion in `X-Shimpz-Supervisor` after validating either its current browser session or the exact
password-and-host-capability reset authority. Its `authority` claim distinguishes `session` from `host-reset`, and
Team admits `host-reset` only on exact Space reset. Team binds the assertion to the canonical request and consumes
it once while retaining an independent machine bearer.
For an authentication-gated Action response, that same signed, one-use assertion may carry one
`assurance` binding containing only the exact reviewed `auth:*` kind and pending challenge ID.
Team requires that binding for the matching authentication challenge and rejects it on every
non-authentication request. Credential and factor material never cross this protocol.

An authenticated Supervisor or Owner may inspect persistent Action input status through
`GET /v1/teams/:team_id/assistant-stored-inputs`. The response is metadata-only: each current
declaration carries exactly `assistant_id`, `stored_input_id`, and `status`; values and generations
never cross HTTP. `DELETE /v1/teams/:team_id/assistant-stored-inputs/:assistant_id/:stored_input_id`
clears only that exact currently declared slot and is idempotent when its value is already absent.
The next Action that needs the slot requests it just in time through the existing human-response
surface. A submitted password is memory-only until the exact Action returns a valid terminal result;
Team then encrypts it for later invocations. Store has no public browser surface for these endpoints.

`vectors.json` contains positive and negative cases that Team, Admin, and Store execute
independently. Generated consumer mirrors pin the producing Teams commit, verify
`contract-files.sha256`, and remain byte-identical to this directory.

Validate the authority from this directory:

```console
python verify.py
```
