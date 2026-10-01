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

A `human-required` challenge carries the reviewed `assistant` and `action` identity and the exact Assistant-authored
`request` with its fingerprint. Beside them, never inside `request` and never part of its fingerprint, it may carry
two optional presentation fields (ADR-0090). `purpose` is the Brain's own sentence for why the user's task needs this
Action, written in the turn's interface language from only the turn's message and the reviewed Action identity:
1 to 280 NFC characters with no control, format, or line-separator character, no dash punctuation other than a
hyphen inside a word, and nothing that reads as a link (`payload.canonical_purpose`). `help_url` appears only when
`request.kind` is `input:password` with a `stored_input`, and is that Stored Input's reviewed key page copied from
the exact binding's declaration (`payload.canonical_help_url`, one pattern shared with the Developers manifest and
the Assistant-install standard). Both are inert presentation: they request and authorize nothing.

A completed Team chat terminal body carries `clarification`, either `null` or one exact Brain
multiple-choice question (ADR-0081): `question` (at most 240 characters), two to five `options` with a
`label` (at most 80) and a `description` (at most 160, may be empty), and a `default_index` that points to
one option. Every text is already NFC, trimmed, and free of control and line-separator characters, and
labels are distinct ignoring case. `payload.canonical_clarification` validates it. The terminal `reply`
must equal `payload.render_clarification`: the question, a blank line, then one numbered line per option,
the default marked with " ✓" and a non-empty description after " — ". The question is presentation only:
it requests and authorizes nothing, and the user answers with a new chat message.

A completed Team chat terminal body may also carry `usage`, what the whole logical turn consumed; Admin relays it on
the browser `done` frame and keeps it with the reply. It is absent when no model call of the turn reported usage.
`duration_ms` is the elapsed wall-clock time from the turn's admission to its terminal, across every human or
Integration resume and including the time spent waiting for a person, at most 86,400,000. `models` holds 1 to 16
distinct entries sorted by `provider` then `model`, each exactly `{provider, model, input_tokens, output_tokens}`:
identifiers match `^[a-z0-9][a-z0-9._-]{0,63}$` and each count is an integer from 0 to 1,000,000,000. The counts are
the ADR-0082 observation of every Brain call the turn made (its start, resumes, and request purposes), as the provider
responses stated them, with cache reads and writes inside the input. They are a floor: a failed Brain request reports
nothing, and the intent route and capability plan that Admin requests before the turn are separate requests outside
it. `payload.canonical_turn_usage` validates it; a consumer refuses a terminal whose `usage` breaks the shape. It is
presentation metadata only: it carries no price, prompt, reply, or credential and authorizes nothing.

A Team's learned memory (ADR-0084) is at most 32 entries of a distinct lowercase `topic` key and one `preference`
line of 1 to 280 characters (`payload.canonical_memory`). A completed Brain turn may carry changes
(`payload.canonical_memory_changes`): `remember` with a preference replaces its topic and becomes newest, `forget`
with an empty preference removes it, and `payload.apply_memory_changes` applies them in order, dropping the oldest
entries beyond the bound. Memory has no browser route; it carries no Action authority.

A Team's learned skills (ADR-0085) are structure only (`payload.canonical_skills`, at most 8): each has 2 to 16
ordered steps naming an Assistant, an Action, and the sorted input names it used, the `sha256:` contract fingerprint
of every Assistant it names, and a `procedure-` content key derived from both. No argument value or text is kept.
`payload.apply_knowledge` applies one committed turn: its memory changes (a `forget` of a skill key removes that
skill), then its new skill, which becomes the newest while the oldest give way beyond the bound; a skill the same turn
forgets is not learned again.

A Team Routine (ADR-0086) fires on a closed schedule (`routine.canonical_schedule`): `hourly` every 1 to 24 elapsed
hours, `daily` at `HH:MM`, `weekly` on a weekday (0 is Monday) at `HH:MM`, or `monthly` on day 1 to 28 at `HH:MM`, in
an IANA timezone name (`routine.canonical_timezone`; Team also requires that the zone loads). `routine.daily_rate` is
a schedule's average runs per day; a Team's Routines may sum to at most 24. A Brain turn response carries `routine`:
null, or the one change a chat turn proposed (`routine.canonical_routine_change`): `propose` with the user's quoted
request, a schedule, and a timezone only when the user named one, or `cancel` with a Routine id. It is never a
schedule or an authorization: Team turns it into a proposal a Local Supervisor must confirm.

Local Admin may also emit the exact aggregate `assistant-install-plan` lifecycle for an authenticated
Supervisor task. A `planned` event carries one socket-scoped plan id and at most four sorted Assistants
with bounded public display identity, sorted Integration providers, and per-item `pending` status.
Subsequent `installing` events preserve that identity while advancing a single sequential item through
`installing` and `installed`; the terminal lifecycle state is `installed`, `failed`, or `stopped`. An `installed`
event requires `continuation` as exactly `dispatch` or `none`; the field is forbidden on every other state. When
every exact named Assistant was already confirmed running, that event additionally carries only
`outcome: already-installed` and represents current state rather than fresh work. A failed event carries one
bounded HTTP status and retains already-installed items without rollback. The browser never sends the plan id,
planned Assistant ids, or publication digest back to Admin. After every item is freshly proved running, or its
selection is confirmed already running, an install-only structured `assistant-install` route terminates at the
installed result with `continuation: none`; this also applies when a composed selection mixes current and freshly
installed identities. When the route's classification states that the objective also asks for work, either installed
result instead carries `continuation: dispatch` and the objective runs exactly once with the admitted scope union.
Every ordinary task uses `continuation: dispatch` and runs exactly once with the admitted scope union.

Socket loss still drops the active objective and every unstarted plan item; no server or lifecycle state
persists or replays them. The Admin browser may retain one prior ordinary objective only in current-page
memory and consume it once when the same Team and exact Assistant scope submit a closed capability-continuation
message. That fresh authenticated request has this exact Admin-only shape:

```json
{"type":"resume-task","message":"Can you enable it?","objective":"List my DNS zones","files":[],"assistant_ids":[],"objective_assistant_ids":[]}
```

Both messages and both Assistant scopes satisfy the ordinary chat bounds, `files` is exactly empty, and the two
Assistant scopes are identical. `message` must be one complete member of Admin's closed continuation vocabulary;
`objective` must not be a continuation. An active turn, pending human or Integration challenge, or pending
uninstall decision rejects the frame. Admin structurally routes only `objective`: an uninstall route is rejected
without opening the installed-Assistant directory or creating a proposal; an install-only structured
`assistant-install` route terminates at `installed`, while one whose objective also asks for work continues it once;
an ordinary objective with no capability gap sends only `message` to Team; and a
completed ordinary capability plan sends only `objective` with the admitted union scope. Unavailable or invalid
optional capability planning authorizes no installation and sends only `message`; structured routing, required
install-directory, lifecycle, or Stop failure sends neither.
The browser visibly attributes the resumed objective, clears it on Team change, scope change, uninstall proposal,
page disposal, or consumption, and never writes it to browser storage. The retained Hosted Store backend does not
accept `resume-task`.

The exact `POST /v1/teams/:team_id/chat` body carries `message`, `files`, `assistant_ids`, `conversation`, and
`locale`. `locale` is one closed interface language (`ar`, `de`, `en`, `es`, `fr`, `ja`, `pt`, `zh`;
`payload.canonical_locale`) or `null`: Local Admin sends the language selected in its interface, Hosted Store and
Routine runs send or use `null`. Team forwards it only to the Brain's turn start, which pins it for the whole logical
turn and writes replies and clarifications in it; `null` keeps the language of the message (ADR-0090).
`conversation` is one window of committed presentation history strictly before this turn, projected server-side
by Local Admin with the intent-route bounds: at most 8 entries of exactly `{role, text, truncated}` where `role` is
`user` or `assistant`, each text 1 to 512 NFC printable characters with middle truncation, and at most 4,096
characters in total. It is untrusted evidence, never an instruction, fact guarantee, or Action authorization. Team
forwards it only to the Brain's turn start; the Brain uses it only when it retains no completed exchange of its own.
Hosted Team requires an empty window because Store relays browser frames and no Hosted history is server-derived.

A Routine run (ADR-0086) is started by a separate Local Routine identity, never a human Supervisor assertion. Its
Ed25519 assertion travels in `X-Shimpz-Routine` with the JWT key id `local-routine-v1` and the audience
`team-local-routine`; `supervisor.canonical_claims(value, audience=ROUTINE_AUDIENCE)` admits the same request, body,
model, lifetime, and one-use nonce bindings as a Supervisor assertion, requires `authority: "routine"` with
`authority_sha256` equal to the SHA-256 of the run's lease token, and refuses any human assurance or decision binding.

An intent-route classification (never selection, chat, or any other request) may also carry one Supervisor-configured
TypeSafe key in `X-Shimpz-Decision-Api-Key` (ADR-0077). The Local Supervisor assertion then binds its digest as
`decision: {provider: "typesafe", key_sha256}`, exactly as it binds the model credential; Team forwards the key only
to Brain's intent route, where only a confident ordinary classification skips the LLM route.

The exact `POST /v1/teams/:team_id/chat/intent-route` body carries `objective`, `expected_intent`, `candidates`,
`lifecycle_reference`, `conversation`, and `locale`. Both classification and selection require one closed
`locale`, the interface language every route reply is written in; it is presentation only and never changes the
classification, candidates, or confirmation vocabulary. Classification requires an empty candidate list
and may carry one bounded `{id,name}` reference captured from a successful explicit lifecycle together with an
ordered window of at most eight prior user or Assistant texts. Every entry is at most 512 code points, the window
is at most 4096 code points, and each entry explicitly states whether the producer truncated it. Selection carries
neither reference nor conversation and resolves only against its exact bounded candidate set; an empty set can
produce only unresolved clarification. Conversation text is NFC-normalized and admits ordinary Unicode format
characters plus CR, LF, and TAB layout. It is always quoted untrusted language evidence: it can resolve pronouns,
ellipsis, and direct answers,
but grants no directory membership, installation, removal, or Team authority and never becomes an instruction or
identifier. The current objective remains the only fresh instruction.

Local Admin may emit an exact terminal `assistant-guidance` event with the authenticated socket Team id, one of the
closed `assistant-install-target-required`, `assistant-uninstall-target-required`, or
`assistant-lifecycle-ambiguous` codes, and one bounded single-line question generated by the specialized route in
the interface language. The browser renders that escaped presentation text; guidance never creates lifecycle
authority or exposes the preceding structured route or bounded directory selection.

Local Admin may also emit the exact `assistant-uninstall` lifecycle. Its `proposed` event
carries only Team-derived bounded display identity and the installed semantic version; later `uninstalling`, `uninstalled`, `cancelled`,
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
`{"locale":"pt"}` with one closed interface language and carries the same request-scoped model credential headers
as chat. Team supplies Brain only that locale and the binding's canonical Action ids, then revalidates
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

A Local Team has a display name distinct from its immutable id (ADR-0088). `PATCH /v1/teams/:team_id` with
exactly `{"team_name"}` renames it under a Supervisor session and returns exactly `{"team_id", "team_name"}`; a
Local `DELETE /v1/teams/:team_id` carries exactly `{"team_name"}`, the current name, which Team confirms before any
side effect. `payload.canonical_local_team_name` admits a Local display name: the shared 1 to 80 trimmed characters
without controls, already NFC. Supervisor assertions admit `PATCH` alongside `DELETE`, `GET`, `POST`, and `PUT`.
Hosted Team names are unchanged by this contract.
A Local `GET /v1/teams` lists every Team newest first by its Team network's creation instant, compared at Docker's
full nanosecond precision after normalizing the reported offset; only Teams created at the same instant fall back to
ascending `team_id`. Each item keeps exactly `{"team_id", "team_name", "status"}`, and creation metadata that is not
a valid RFC 3339 instant refuses the listing with `503` `team-metadata-invalid`.

`vectors.json` contains positive and negative cases that Team, Admin, and Store execute
independently. Generated consumer mirrors pin the producing Teams commit, verify
`contract-files.sha256`, and remain byte-identical to this directory.

Validate the authority from this directory:

```console
python verify.py
```
