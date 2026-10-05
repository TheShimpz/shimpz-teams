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

A `human-required` challenge carries the reviewed `assistant` and `action` identity and the exact canonical
Assistant `request` with its fingerprint: every copy field is a catalog reference `{"message": id, "params": {...}}`
(Assistant Spec v1), so the fingerprint never depends on the display language. Beside it, never inside `request` and
never part of its fingerprint, the challenge carries three required localization fields (ADR-0091): `locale`, the one
concrete closed interface language (`payload.canonical_locale`, never `null`) the challenge was created for;
`pack_digest`, the `sha256:` digest of the reviewed binding's language pack (`payload.canonical_pack_digest`); and
`rendered`, the display text of exactly the request's copy fields in that locale (`payload.canonical_rendered`):
`title` (at most 80 characters) and `description` (500); `label` (80) for an input; `placeholder` (120, `null`
exactly when the request's placeholder is `null`) for a text, textarea, password, or phone input; and, for a choice,
`options` in request order, each exactly `{label, description}` (80 and 160, `description` `null` exactly when the
request option's is). Rendered text is trimmed, printable, NFC, and within its bound without truncation. Team renders
it from the English catalog or the pack, inserting each parameter once; Admin verifies the canonical fingerprint and
validates this projection, while request kinds, option values, and the authorization scope stay canonical. A live
challenge binds the canonical fingerprint, the exact binding, the catalog and pack digests, and the locale; a
different locale needs a fresh challenge. Opening a frozen Routine run's challenge carries the Admin interface
language as exactly `{"locale": "pt"}` (`routine.canonical_challenge_open`, never `null`). Local Admin opens the
Team's pending chat challenge with the same exact body at `POST /v1/teams/:team_id/chat/human/challenge`
(Local only), and a chat body that names a locale reopens a pending challenge the same way. Team answers
`{team_id, status: "none"}` when nothing is pending and returns a challenge already in that locale unchanged.
Otherwise, as for a Routine opening, it re-renders the same canonical request and fingerprint from the same binding's
pack under Team's lock as a fresh challenge with a new `challenge_id` and the earlier expiry, and the earlier id stops
answering at once. The turn keeps its own language and a purpose its origin locale. A binding whose catalog or pack
changed ends the paused turn; Admin refuses an opened challenge whose `locale` is not the one it asked for.

A staged Local snapshot's summary follows the interface language too (ADR-0091). Local Admin reads it at
`GET /v1/local-assistants/:image_hash/summary/:locale` (Local only), where `locale` is one closed interface language;
Team answers exactly `{locale, summary}` (`payload.canonical_snapshot_summary`): the snapshot catalog's English summary
for `en`, otherwise that one message's translation from the snapshot's own pack, admitted complete for its own catalog
and read from the immutable image without starting it. The summary is at most 160 trimmed, printable, NFC characters;
no request copy, catalog, or pack is ever returned. Admin refuses an answer whose `locale` is not the one it asked for.
The read shares the bounded icon preview: while extraction capacity is busy Team answers 503
`local-assistant-preview-busy` with `retry_after_ms`.

The challenge may also carry two optional presentation fields (ADR-0090). `purpose` is the Brain's own sentence for
why the user's task needs this Action, projected only when its recorded origin locale equals the challenge `locale`,
so a Routine challenge shows its localized scope without a purpose. It is written in the turn's interface language
from only the turn's message and the reviewed Action identity:
1 to 280 NFC characters with no control, format, or line-separator character, no dash punctuation other than a
hyphen inside a word, and nothing that reads as a link (`payload.canonical_purpose`). `help_url` appears only when
`request.kind` is `input:password` with a `stored_input`, and is that Stored Input's reviewed key page copied from
the exact binding's declaration (`payload.canonical_help_url`, one pattern shared with the Developers manifest and
the Assistant-install standard). Both are inert presentation: they request and authorize nothing.

An authorization challenge (`approval`, `auth:password`, `auth:totp`, or `auth:passkey`) of an Action that declares a
file input also carries `file`, the platform-controlled disclosure of the one selected file whose original bytes, with
any metadata embedded in them, only the approved replay delivers to that Action (ADR-0093): exactly `{id, name,
media_type, size, sha256}` with the opaque file id, the literal filename, the Team-determined media type, a size of at
most 8 MiB, and the original lowercase SHA-256 (`payload.canonical_file_disclosure`). The filename is literal data that
Admin renders as text, never a Creator translation parameter. Team binds the disclosed file to the challenge and
delivers only bytes with that size and digest; any other challenge carries no `file`.

A completed Team chat terminal body carries `clarification`, either `null` or one exact Brain
multiple-choice question (ADR-0081): `question` (at most 240 characters), two to five `options` with a
`label` (at most 80) and a `description` (at most 160, may be empty), and a `default_index` that points to
one option, or `null` when no option is recommended or preselected (every Routine question, ADR-0092 amendment
2026-10-05); a question with a `null` default may offer a single option. Every text is already NFC, trimmed, and free of control and line-separator characters, and
labels are distinct ignoring case. `payload.canonical_clarification` validates it. The terminal `reply`
must equal `payload.render_clarification`: the question, a blank line, then one numbered line per option,
a recommended default marked with " ✓" (none for `null`) and a non-empty description after " — ". The question is presentation only:
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

A completed Team chat terminal body may also carry `restricted_actions` (ADR-0093): while readable attachment content
(text or an image) was in the turn, Team offered and admitted only Actions that declare an authorization capability,
and this names the selected Assistants' Actions it withheld for that reason, so Admin can explain them in the
interface language with an attachment-free next step. It is exactly `{actions, total}`: `actions` lists 1 to 16
distinct `{assistant, action}` identities in identity order, the first of all withheld that fit; `total` counts every
withheld Action, from the listed count up to 2,048; the canonical JSON is at most 2,048 bytes
(`payload.canonical_restricted_actions`). It is absent when nothing was withheld, including every turn
whose attachments were all opaque, and a resumed turn reports what its final segment withheld. It is presentation
only: it never resends a message, removes an attachment, or grants any Action.

A Team's learned memory (ADR-0084) is at most 32 entries of a distinct lowercase `topic` key and one `preference`
line of 1 to 280 characters (`payload.canonical_memory`). A completed Brain turn may carry at most 40 changes
(`payload.canonical_memory_changes`), enough to forget every memory and every skill at once: `remember` with a
preference replaces its topic and becomes newest, `forget` with an empty preference removes it, and
`payload.apply_memory_changes` applies them in order, dropping the oldest entries beyond the bound. A Brain refuses a
proposal that would exceed the bound instead of truncating, and Team refuses a longer change list whole. Memory has no browser route; it carries no Action authority.

A Team's learned skills (ADR-0085) are structure only (`payload.canonical_skills`, at most 8): each has 2 to 16
ordered steps naming an Assistant, an Action, and the sorted input names it used, the `sha256:` contract fingerprint
of every Assistant it names, and a `procedure-` content key derived from both. No argument value or text is kept.
`payload.apply_knowledge` applies one committed turn: its memory changes (a `forget` of a skill key removes that
skill), then its new skill, which becomes the newest while the oldest give way beyond the bound; a skill the same turn
forgets is not learned again.

A Team Routine (ADR-0086) fires on a closed schedule (`routine.canonical_schedule`): `hourly` every 1 to 24 elapsed
hours, `daily` at `HH:MM`, `weekly` on a weekday (0 is Monday) at `HH:MM`, or `monthly` on day 1 to 28 at `HH:MM`, in an
IANA timezone name (`routine.canonical_timezone`; Team also requires that the zone loads), or, only when the user asks
for it, `continuous`: its next run is due `gap` seconds (5 to 86,400) after the previous one ended, never overlapping,
with at most `cap` (1 to 1,000) starts in any rolling 24 hours (ADR-0092). `routine.daily_rate` is a schedule's runs per
day and `routine.daily_cap` its whole rolling 24-hour cap; a Team's Routines' caps may sum to at most
`routine.MAX_DAILY_RUNS` (1,000), which also bounds the Team's starts in any rolling 24 hours, whatever Routine made
them. A Routine is created or changed only from the authenticated user's own chat message, without a confirmation card
(ADR-0092): Team validates the Brain's compiled change against that message and the exact installed contracts, and
commits the Routine, its notice, and the request's receipt together with the reply. That notice has the Routine outcome
`created` or `changed`, no run id, and exactly `{name, steps, schedule, timezone}` (`routine.canonical_notice`): the
Routine's name (`routine.canonical_name`, 1 to 80 NFC printable characters on one line), its plan's safe projection, its
schedule, and its zone. The projection (`routine.canonical_steps`) is 1 to 8 ordered steps of exactly `{id, assistant,
action, inputs, stored_inputs}`: each input, sorted by member, is a `literal` whose `value` is `routine.literal_preview`
of its JSON (at most 120 characters, every control or invisible character escaped), a `run_clock` whose `value` is its
format, or a `step_output` naming an earlier step and an RFC 6901 pointer; `stored_inputs` names the Stored Inputs the
step's Action uses by id only, never a value. The projection encodes to at most `routine.MAX_STEPS_BYTES` (96 KiB);
Team refuses a plan whose projection is larger, never truncating it. The Routine view a Supervisor lists
(`routine.canonical_routine_view`) carries the same name and projection. `GET /v1/teams/:team_id/routines` answers the
Team's whole list, every Routine, live run, and unresolved incident, within `routine.MAX_ROUTINE_LIST_BYTES`; it is the
only Team answer above the Local API's 128 KiB response cap. Team also keeps, never on the wire, the evidence of the request that granted each
revision: its receipt, revision, plan digest, a commitment to the message, the quote's span, each input's validated
provenance, and any answer a bound Routine question selected.

A run has one notice, keyed by its run id, whose version grows as the run goes on (`routine.canonical_notice_detail`
closes each outcome's detail). `done` and `recovered` name the ordered `actions`, `[assistant, action]` pairs of the
steps it carried out, never their input or result; `recovered` is a run that a continuation completed after a hold.
`held` names the step whose effect is unresolved as `{assistant_id, action}`, both `null` when the run sealed no plan
cursor; the same run's notice then goes on as `paused`, the same step plus a `reason` (`decided`, `unavailable`,
`exhausted`, `policy`, or `evidence`, recovery evidence that could not be read), or `user-skipped` when a person set
the run aside, the same step plus the `choice` that did it (`run`, `recreate`, or `delete`, a deletion of its Routine).
A person's `user-skipped` is a run outcome; the Routine outcome `skipped` reports missed firings and
has no run id. A continuous Routine's healthy runs, each completed with no earlier notice, share one versioned `healthy`
Routine notice per minute bucket instead: its instant is the minute's start and its `runs`, at most
`routine.MAX_ROLLUP_RUNS`, counts them and is also its version. Every other outcome stays one notice per run. The rollup
minute only moves forward: a run whose clock fell back into an earlier minute keeps its own notice. A Routine change
keeps its minute's count, and the `routine_rollup_delivery` vectors pin exact delivery sequences, with the transcript
rows Admin must end with. `failed` names its code and the Actions that completed; a run whose failed step may have acted
is held instead.

A Supervisor's `GET /v1/teams/:team_id/routines` lists each Routine (`routine.canonical_routine_view`, whose `paused`
says dispatch is off), its live runs (`routine.canonical_run_view`), and its unresolved `incidents`, at most
`routine.MAX_UNRESOLVED_INCIDENTS` (`routine.canonical_incident_view`): each held run's id, Routine, quote, creation
instant, and step, which outlive a deleted Routine. `POST /v1/teams/:team_id/routines/incidents/:incident_id/card` with
`{}` opens that run's recovery card (`routine.canonical_card`): the step it stopped at and its `step` ordinal of
`steps` in the plan the run executed, that revision, the `evidence` of its failure (`recorded`, with the held
operation's latest sanitized `diagnostic`; `absent` when none is kept; or `unavailable` when it could not be read), a
one-use 32-hex `nonce`, `expires_in` of 300 seconds, and exactly the choices `run`, `recreate`, and `delete` in that
order, none recommended. The card is bound to the authenticated person, the Team incarnation, the Routine and its
current revision, the run, its operation, and the Routine's sealed creation source. `POST .../answer` with exactly
`{nonce, choice}` (`routine.canonical_card_answer_request`) answers it once with `run` or `recreate`; `delete` is never
a card answer but the Routine's own confirmed deletion. `routine.canonical_card_answer` says what it did. Rodar
(`run`) sets the held run aside without verifying it and requests one fresh run of the current revision, answering
`requested`; it carries no model credential. Recriar (`recreate`) carries the private model credential, which the
assertion binds, compiles the Routine's sealed creation message from scratch, and replaces the Routine in place as its
next revision, answering `recreated`. Both refuse while the held attempt's workload is not proven stopped
(`routine-workload-unquiesced`), while another run of the Routine is live (`routine-busy`), or once it is deleted
(`routine-not-found`); Rodar also refuses when the Routine's Assistant contracts changed
(`routine-contracts-changed`), and Recriar when its source is gone (`routine-source-unavailable`), when the compile
asks about a field its source never selected or refuses (`routine-recreate-refused`), or when the compile could not
run (`routine-recreate-unavailable`). Anything refused changes nothing. An expired, foreign, or reused card is
`routine-card-expired`, and one whose Routine revision, Team incarnation, held generation, operation, or creation
source changed since it opened is `routine-card-stale`; every answer is checked and applied in the Team's execution
slot, and its write checks the same state again. `POST /v1/teams/:team_id/routines/:routine_id/pause` with `{}` turns
a Routine's dispatch off, answering `paused` true, while a run already going finishes;
`POST /v1/teams/:team_id/routines/:routine_id/resume` with `{}` turns dispatch back on and starts a fresh failure
streak; an unresolved incident still holds the Routine until its card settles it. Deleting a Routine sets every one of
its unresolved incidents aside.

A Local Supervisor reads one Routine run's execution details (ADR-0092) with `GET
/v1/teams/:team_id/routines/runs/:run_id/diagnostics`, answered by `routine.canonical_diagnostics`: the Team and run ids
and at most 32 diagnostics, oldest first, one per attempt of one logical operation (`operation_id`, the version 4 UUID
Team journaled, and `attempt` from 1 to 64), each naming its Assistant Action and recording instant. Each holds exactly
one of a `failure`, the Team-sanitized handled failure (`error_type`, `message`, `provider`, `http_status`,
`response_excerpt`, and the `redacted` and `truncated` flags, with the Assistant Spec bounds), or a `condition`, the
safe transport condition (`exit-status:<code>`, `stderr-output`, `timeout`, `frame-invalid`, `exit-unavailable`, or
`transport-failed`); raw child output is never reflected. Text is literal evidence that Admin renders escaped, never as
Markdown or HTML, and a diagnostic is never effect proof or authority. Team keeps these bodies encrypted for at most
seven days and 10 MiB per Team, readable only by the same Team incarnation; deleting the Routine or the Team removes
them.

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
A Local chat body also carries `request` and `timezone` (`payload.LOCAL_CHAT_BODY_FIELDS`, ADR-0092); Hosted keeps
the exact body above. `request` is the identity Local Admin issues once per sent message
(`payload.canonical_request_identity`): `issued_at`, a whole UTC epoch second, and `nonce`, 32 lowercase hex. Admin
returns the browser an authenticated seal of it; an ADR-0081 resend of that message carries the seal back, and Admin forwards the original identity only while `payload.request_identity_fresh`
admits it, so an expired retry is never a new grant. Team binds it to the Supervisor
principal, the Team incarnation, and the canonical message, and a Routine change carried by the request commits at most
once with it: only while `issued_at` is less than 900 seconds old and at most 60 seconds ahead of Team's clock
(`payload.request_identity_fresh`, exclusive at 900 s, the same second the receipt stops being live), and only while
the Team holds fewer than 256 live receipts; expiry and saturation refuse the change and never evict a valid
receipt. `timezone` is the browser's IANA zone name (`routine.canonical_timezone`) or `null`; Team uses it only as the
default zone of a Routine the message creates.

A Routine run (ADR-0086) is started by a separate Local Routine identity, never a human Supervisor assertion. Its
Ed25519 assertion travels in `X-Shimpz-Routine` with the JWT key id `local-routine-v1` and the audience
`team-local-routine`; `supervisor.canonical_claims(value, audience=ROUTINE_AUDIENCE)` admits the same request, body,
model, lifetime, and one-use nonce bindings as a Supervisor assertion, requires `authority: "routine"` with
`authority_sha256` equal to the SHA-256 of the run's lease token, and refuses any human assurance or decision binding.
Admin's scheduler claims under the Team bearer with `POST /v1/routines/claim` and exactly `{}`
(`routine.canonical_claim_request`): no model key gates a claim, because a healthy compiled run needs none (ADR-0092),
and any Team with a configured model may be claimed. The answer (`routine.canonical_claim`) is one run with its lease
token, lease expiry, the Team's configured provider, and the Routine `revision`, `plan_digest`, and `mode` (`scheduled`
or `continuous`, `routine.RUN_MODES`) it was claimed at, or `null` with `next_due_at`, the earliest epoch second a
Routine of a Team Admin can run becomes due (`null` when none will), so Admin wakes then while still reconciling on its
own interval. The run's signed segment request, `POST /v1/teams/:team_id/routines/runs/:run_id/segment`, carries exactly
that `{revision, plan_digest, mode}` (`routine.canonical_segment_request`); any other is refused as
`routine-revision-stale` before anything runs. The run's segment and its frozen answers carry the private model
credential only when Admin holds the Team's key; it then travels whole and the assertion binds it, and without it a held
run's recovery pauses as `unavailable`.

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

An installed Assistant's summary follows the interface language (ADR-0091). Admin reads it at
`GET /v1/teams/:team_id/assistants/:assistant_id/summary/:locale`, where `locale` is one closed interface language,
and Team answers the same closed `{locale, summary}` as a staged snapshot's summary
(`payload.canonical_snapshot_summary`): the current binding's English catalog summary for `en`, otherwise that one
message's translation from the pack verified against the binding's `pack_digest`. No request copy, catalog, or pack is
ever returned. A missing binding fails as absent, and a missing or mismatched pack fails closed. Admin refuses an answer
whose `locale` is not the one it asked for.

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
