# Team HTTP protocol v1

Team owns the closed identifiers, payload projections, and WebSocket frame boundary used by Admin.
`payload.py` validates Team-facing HTTP values without trusting upstream fields.
`identifiers.py` owns the closed Team, Assistant, and Action identifier grammars, `purpose.py` the Action purpose
sentence rule, and `turn.py` the chat-turn bounds (the user message, a clarification question, memory and skill
changes, a skill's content key, the reply, the Action requests one suspension carries, capability planning, intent
routing, and the attachment content of a turn) and the opaque thread, interrupt, and model id grammar Team and the
Brain exchange (`SAFE_ID_RE`); `payload.py` re-exports all three, and the Brain
consumes these three files as a pinned mirror. Team, the Brain, and Admin each bind a shared bound to its one
definition here or in `payload.py`, never to a copied literal.
`challenge.py` owns the presentation a human-required challenge carries beside its canonical request: the rendered
copy, the one disclosed file, and an authorization's input projection. Only Team and Admin consume it, so `payload.py`
never imports it.
`preview.py` owns the framing of a Team file's preview, the sanitized image rendition of an image or of one PDF page
Team sends a Supervisor's browser, and `content.py` the framing of a file's verified original Team sends it to
download; only Team and Admin consume them.
An Assistant id (at most 40 characters) and an Assistant's Integration, provider, or Stored Input identifier
(`canonical_identifier`, at most 64) use the Developers published-Assistant grammar; an Action id uses Team's wider
grammar, which also admits `.` and `_` separators, within 128 characters.
`websocket.py` validates the bounded `shimpz.chat.v7` frame primitives and redacts unsafe errors.
`progress.py` owns the closed metadata-only progress events and NDJSON terminal framing used by
Local Team chat. An Action occurrence carries only its canonical reviewed Assistant and Action
identifiers; it never carries arguments, results, prompts, model output, or free text. Progress is
advisory; only the single terminal record determines the operation outcome. A missing, repeated,
malformed, oversized, or out-of-order record fails closed at the consumer without widening Team
authority or exposing execution payloads.
Team Routine forms are split by responsibility (ADR-0101): `routine.py` owns schedules, timezones, plans, and outputs;
`routine_notice.py` notices and the views Admin lists; `routine_run.py` recovery cards, claims, segments, diagnostics,
and run steps; `routine_proposal.py` the confirmation card, Team's questions, refusals, answers, and the output choices;
and `routine_context.py` the Routine listing and rerun work a recording chat's Brain receives. The Brain mirrors
`payload.py`, `phrase.py`, `routine.py`, `routine_notice.py`, `routine_proposal.py`, and `routine_context.py` with the
three files above.
`strict_json.py` is an exact copy of the umbrella `.standards/strict_json.py` security source: the frame and stream
decoders parse through it, so a duplicate field and every non-finite number, including an exponent overflow such as
`1e999`, fail closed. The modules import one another as a package, and flat when `verify.py` runs them as scripts.
Thread pools, queues, worker limits, and saturation behavior are deployable-owned runtime policy,
not part of this wire protocol.

`shimpz.chat.v7` retains the Local Admin's exact `human-response` client frame. It binds a `submit` or
`deny` decision to one opaque lowercase 32-hex challenge. Submitted values admit only `true`, one
bounded string, or one bounded unique string list. The pending reviewed descriptor determines the
actual request kind and tighter bounds; the Team revalidates it authoritatively. For Local
`auth:password`, the browser submits the password only to Admin, Admin replaces it with `true` after
verification, and the signed Local assertion binds the successful assurance to the same challenge.
Authentication factor material never crosses to Team, Brain, an Assistant, or a progress event.

A `human-required` challenge carries the reviewed `assistant` and `action` identity and the exact canonical
Assistant `request` with its fingerprint: every copy field is a catalog reference `{"message": id, "params": {...}}`
(Assistant Spec v1), so the fingerprint never depends on the display language. Beside it, never inside `request` and
never part of its fingerprint, the challenge carries three required localization fields (ADR-0091): `locale`, the one
concrete closed interface language (`payload.canonical_locale`, never `null`) the challenge was created for;
`pack_digest`, the `sha256:` digest of the reviewed binding's language pack (`payload.canonical_pack_digest`); and
`rendered`, the display text of exactly the request's copy fields in that locale (`challenge.canonical_rendered`):
`title` (at most 80 characters) and `description` (500); `label` (80) for an input; `placeholder` (120, `null`
exactly when the request's placeholder is `null`) for a text, textarea, password, or phone input; and, for a choice,
`options` in request order, each exactly `{label, description}` (80 and 160, `description` `null` exactly when the
request option's is). Rendered text is trimmed, printable, NFC, and within its bound without truncation. Team renders
it from the English catalog or the pack, inserting each parameter once; Admin verifies the canonical fingerprint and
validates this projection, while request kinds, option values, and the authorization scope stay canonical. A live
challenge binds the canonical fingerprint, the exact binding, the catalog and pack digests, and the locale; a
different locale needs a fresh challenge. Opening a frozen Routine run's challenge carries the Admin interface
language as exactly `{"locale": "pt"}` (`routine_run.canonical_challenge_open`, never `null`). Local Admin opens the
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
and read from the immutable image without starting it. The summary is at most 80 trimmed, printable, NFC characters;
no request copy, catalog, or pack is ever returned. Admin refuses an answer whose `locale` is not the one it asked for.
The read shares the bounded icon preview: while extraction capacity is busy Team answers 503
`local-assistant-preview-busy` with `retry_after_ms`.

A staged snapshot's whole Assistant page follows the interface language too. Local Admin reads it at
`GET /v1/local-assistants/:image_hash/details/:locale` (Local only), from the same bounded preview, with the same
busy answer and errors. Team answers the closed Assistant details object (`payload.canonical_assistant_details`) plus
`trace_id`:

```json
{"locale": "pt", "assistant_id": "shimpz-cloudflare", "assistant_version": "1.4.0", "name": "Shimpz Cloudflare",
 "creators": ["@roxygens"], "summary": "Publica alterações de DNS com segurança.",
 "description": "Navegue pelas zonas da sua conta Cloudflare e publique registros DNS somente depois da sua aprovação.",
 "links": {"site": "https://shimpz.com/", "github": "https://github.com/TheShimpz"},
 "actions": [{"id": "list-zones", "effect": "read_only", "description": "Liste suas zonas do Cloudflare."}],
 "integrations": [{"id": "cloudflare", "provider": "cloudflare"}],
 "stored_inputs": [{"id": "api-token", "label": "Token da API",
   "description": "Crie um token de API no painel da Cloudflare, com acesso de edição de DNS, e copie-o.",
   "help_url": "https://dash.cloudflare.com/profile/api-tokens"}]}
```

`locale` is the closed interface language asked for; `assistant_id` and `assistant_version` follow the Developers
grammars; `name` is 1 to 80 characters; `creators` are 1 to 16 unique self-declared `@handle`s (a staged snapshot's
first four), never identity authority; `summary` (80), `description` (500), each Action `description` (120), and each
Stored Input `label` (120) and help-text `description` (500, `payload.canonical_stored_input_help`) are rendered text: the English catalog text for `en`, otherwise that message's translation
from the snapshot's or binding's own pack, trimmed, printable, and NFC within the bound. `links` holds zero to six
unverified Creator links (`payload.canonical_creator_links`): kinds `site`, `github`, `x`, `youtube`, `linkedin`, and
`instagram`, each a `help_url`-grammar URL of at most 256 characters on its kind's host (`site` any public host,
`github` `https://github.com/`, `x` `https://x.com/`, and `youtube`, `linkedin`, and `instagram` on their `.com` host
with or without `www.`). `actions` (1 to 128, `effect` `read_only` or `mutating`), `integrations` (0 to 16, each its
provider identifier), and `stored_inputs` (0 to 8) are each sorted by unique identifier `id`. Each Stored Input also carries its declared
`help_url` (`payload.canonical_help_url`), the official page where a person gets the value; Admin shows it as the one
link beside the help text. No catalog, pack, schema, or request copy is returned. Admin refuses an answer whose `locale` is not the one
it asked for.

Local Admin lists the staged Local snapshots at `GET /v1/local-assistants` (Local only). Team answers
`{"assistants": [...]}` plus `trace_id`, at most 50 entries, one per staged identity (each Assistant's one
`shimpz-local/<assistant_id>:staged` image), and each entry is exactly `payload.canonical_local_assistant`:

```json
{"assistant_id": "fixture-assistant", "assistant_version": "0.1.0", "name": "Fixture Assistant",
 "summary": "Exercise immutable admission.", "declared_creators": ["@fixture"], "actions": ["ping"],
 "integrations": [], "image_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
 "platform": "linux/amd64", "created_at": "2026-08-28T17:00:00Z", "provenance": "local", "unpublished": true,
 "admission": "inadmissible", "reason": "declaration-invalid"}
```

The identity, `name`, `summary` (each 1 to 80 trimmed characters without a control character), the first four
self-declared `declared_creators`, and the sorted unique `actions` (1 to 128) and Integration providers (0 to 16) are
the image's labels: unverified Local presentation. `admission` is Team's verdict for the exact image (ADR-0060):
`admissible` when the full install admission, which includes every preview check, accepts it; `inadmissible` when it
refuses it, with `reason` `declaration-invalid` (the declaration fails the current contract) or `image-inconsistent`
(the image's labels or files disagree with its own declaration); `unavailable` when Docker could not answer this time.
`reason` is null for every other verdict, and no entry carries the snapshot's content. A staged image whose
`org.shimpz.assistant.id` label is a valid Assistant id but whose other stage labels or fixed image metadata fail
validation is `inadmissible` with `reason` `labels-invalid`, and its `assistant_version`, `name`, `summary`,
`declared_creators`, `actions`, `integrations`, and `created_at` are null; a staged image without a valid Assistant id
label has no identity and is left out of the inventory. One such image never fails the inventory. Only an admissible snapshot may
be offered or installed; every other entry still names its staged identity, so a consumer never offers a publication
of the same `assistant_id` in its place. Team reuses a settled verdict for the exact image id and tag set and never
reuses an unavailable one.

The challenge may also carry presentation fields beside the fingerprinted `request` (ADR-0090). `purpose` is the Brain's own sentence for
why the user's task needs this Action, projected only when its recorded origin locale equals the challenge `locale`,
so a Routine challenge shows its localized scope without a purpose. It is written in the turn's interface language
from only the turn's message and the reviewed Action identity:
1 to 280 NFC characters with no control, format, or line-separator character, no dash punctuation other than a
hyphen inside a word, and nothing that reads as a link (`payload.canonical_purpose`). `help` and `help_url` appear
exactly when `request.kind` is `input:password` with a `stored_input`, and then both are required: `help` is that
Stored Input's help text rendered in the challenge `locale` from the exact binding's pack
(`payload.canonical_stored_input_help`), and `help_url` is its reviewed help link copied from the exact binding's
declaration (`payload.canonical_help_url`, one pattern shared with the Developers manifest and the Assistant-install
standard). A Stored Input challenge without both is refused. All three are inert presentation: they request and
authorize nothing, and an Assistant runtime request can never supply or override them.

An authorization challenge (`approval`, `auth:password`, `auth:totp`, or `auth:passkey`) of an Action that declares a
file input also carries `file`, the platform-controlled disclosure of the one selected file whose original bytes, with
any metadata embedded in them, only the approved replay delivers to that Action (ADR-0093): exactly `{id, name,
media_type, size, sha256}` with the opaque file id, the literal filename, the Team-determined media type, a size of at
most 8 MiB, and the original lowercase SHA-256 (`challenge.canonical_file_disclosure`). The filename is literal data that
Admin renders as text, never a Creator translation parameter. Team binds the disclosed file to the challenge and
delivers only bytes with that size and digest; any other challenge carries no `file`.

Every chat authorization challenge, a declared `approval`, `auth:password`, `auth:totp`, or `auth:passkey`, also
carries `input`, the platform-rendered projection of the Action's validated input
(`challenge.canonical_input_projection`): exactly `{fields, omitted}`, where `fields` holds at most 16 rows
`{name, value, truncated}` in strictly ascending `name` order, one per top-level argument. `name` (1 to 128
characters) is the argument's name and `value` (1 to 400) its canonical JSON text, so a string stays quoted; both are
printable because every other character is escaped as a visible `\uXXXX`. `truncated` is true when either had to be
cut to its bound, and `omitted` counts the arguments past the sixteenth row (it is nonzero only with 16 rows). Admin
renders the rows as literal text and must show both a cut row and the count of omitted arguments, so no argument is
ever hidden silently. A Routine run's challenge and any input request carry none (ADR-0115). Team adds no confirmation
of its own: an Action's declared human requests are its only ceremony (ADR-0046).

A completed Team chat terminal body carries `clarification`, either `null` or one exact Brain
multiple-choice question (ADR-0081): `question` (at most 240 characters), two to five `options` with a
`label` (at most 80) and a `description` (at most 160, may be empty), and a `default_index` that points to the one
recommended option, or is `null` when no option is recommended: a question about a Routine steers no choice, so the
Brain sends it with `null` (ADR-0101). Every text is already NFC, trimmed, and free of control and line-separator characters, and
labels are distinct ignoring case. `payload.canonical_clarification` validates it. The terminal `reply`
must equal `payload.render_clarification`: the question, a blank line, then one numbered line per option,
the recommended default, when there is one, marked with " ✓" and a non-empty description after " — ". The question is presentation only:
it requests and authorizes nothing, and the user answers with a new chat message.
Admin composes that message from the original request, a blank line, then the question and the answer on their own
lines, each after its interface-language label (`payload.CLARIFICATION_LABELS`, `payload.compose_clarified`); a request
may be clarified more than once. `payload.authored_segments` reads the person's own words back out of such a message as
segments in order: the original text, then each answer without its label, never a question; a later segment is the
person's later word (ADR-0101).

A Local chat terminal that recorded a Routine (ADR-0101) carries at most one of `routine_proposal`,
`routine_question`, and `routine_refusal` beside the agent's own `reply`, which keeps the work the turn already did.
`routine_refusal` (`routine_proposal.canonical_refusal`) is exactly `{code}`, a closed-grammar code that Admin words in the
interface language (for example `routine-step-budget` or `routine-secret-literal`); nothing was created.
`routine_question` (`routine_proposal.canonical_question`) is `{code, options, value}`: Team asks the person before any card,
the recording is kept, and the person's answer is an ordinary chat message. Its code is one of
`routine_proposal.QUESTION_CODES`: how often it runs (`routine-schedule-unstated`), what each run does with its result (`routine-output-unstated`:
Admin offers `routine_proposal.OUTPUT_CHOICES`, one label for each of `routine.OUTPUT_MODES` in the interface language, and
the label the person picks states that choice), a stated interval the Team's daily budget
cannot hold (`routine-interval-over-budget`, whose `value` is the shortest interval in seconds that fits; with room
for no run at all the recording is refused as `routine-step-budget`), which item an input means (`routine-binding-ambiguous`, whose `options` are at most 8
targets `{value, label}`: `value` is the exact compact JSON text of the string or integer the input would take, so
no client rounds a large integer, and `label` the item's name, or `null`), and work to run again
(`routine-work-rerun`), for a value that no earlier result provides or a target the person chose instead. Only `routine-binding-ambiguous` has options, and only
`routine-interval-over-budget` has a value. The schedule, over-budget, and output questions are the settings
questions (`routine_proposal.SETTINGS_QUESTION_CODES`): an answer to one runs no work unless it changes the work, and
then runs the whole recurring work it results in once. When the person's next send is Admin's composed answer to an ambiguous
binding and its latest answer is exactly one target's JSON text that states no schedule, Team records again with the
request's stored intent without asking the Brain, and its reply is empty. Every other answer goes to the Brain, which
records again with a description that says it; a settings answer (`routine_settings_answer` below) runs no Action
again and keeps the work its intent selected, and its reply is empty too. A record call whose send ran no Action and
that replaces exactly what the stored intent replaces continues that intent whatever it names the Routine: it keeps
the intent's work and the revision its replaced Routine was shown at. A completed turn's `reply` is empty only beside a `routine_proposal`,
`routine_question`, or `routine_refusal`, which then says everything the person reads.

What a person's own words state about a Routine is read here too, with no model, so Team and the Brain read the
same words alike (`phrase.py`, ADR-0101): `phrase.stated` reads the canonical schedules a text states,
`phrase.outputs` the output choices (`show`, `changes`, `none`), and `phrase.zones` the exact IANA zones it
names. `phrase.team_asks(text, language)` is true when one clarification question or option label asks or states a
schedule, an interval, an output choice, or how many times a Routine may run, which Team asks itself and the Brain
never does.
`phrase.requests_routine(text)` is true when the text names a Routine affirmatively ("cria uma rotina", "create a
routine"), so a chat that asks for one is about a Routine before any schedule is stated.
The tables cover the eight interface languages. A sentence that asks states nothing; a negation in any
language rejects every reading it reaches, except that `phrase.stated(text, language)` and
`phrase.team_asks(text, language)` read a text known to be written in that interface language, such as a description
or a clarification the Brain's model writes in the chat's language, with only that language's negations, so
Portuguese "no Cloudflare a cada 30 segundos" keeps its schedule. "Show the result" qualified by "only when it
changes" in its own clause is that one choice, and so is showing it whenever it changes ("mostrar sempre que mudar"),
while alternatives, or a condition on something else, offer both. Vectors pin a reading for each language and kind.

The Team→Brain Routine forms of a recording chat are defined here too, so the Brain mirrors them instead of copying
them (ADR-0101). `routine_context.canonical_routine_listings` admits the Team's Routine listing: at most
`routine.MAX_ROUTINES` entries, no Routine twice, each `routine_context.canonical_routine_listing` `{routine_id, name, schedule,
timezone, timezone_source, revision, daily_steps, output, steps}`, where `daily_steps` is 1 to
`routine_context.MAX_LISTED_DAILY_STEPS`, `output` is `{mode}` (a shown mode shows the last step), and each of at most 256 steps is `{id,
assistant, action, inputs}` with its plan id (`routine_context.ROUTINE_STEP_ID_RE`) and at most 64 sorted input member names,
never a value. The pending question the Brain sees is `routine_proposal.canonical_question`, exactly as the reply carried it.
`routine_context.canonical_rerun` admits the work a pending rerun question asks the Brain to repeat: 1 to
`routine_context.MAX_RERUN_ENTRIES` entries `{assistant, action, count, inputs}` in order, whose counts sum to at most
`routine_context.MAX_RERUN_CALLS`; each has at most `routine_context.MAX_RERUN_INPUTS` inputs in member order, each `{member, kind,
value, chosen, source}` with a plain member of at most `routine_context.MAX_RERUN_MEMBER_CHARS` characters. A `value` input
carries its exact compact JSON text of at most `routine_context.MAX_RERUN_LITERAL_CHARS` characters, or `null` when withheld,
and whether it is a target the person chose; a `clock` input is the run's date; a `fresh` input never shows its value
and names the `{assistant, action}` whose earlier result held it, or `null`. Team sends no such work past these
bounds. `routine_mode`, whether the chat is about a Routine, is a plain advisory boolean. `routine_schedule` is the one
schedule Team reads from the person's sends (`routine.canonical_schedule`) and `routine_output` the one output mode it
reads (`routine.OUTPUT_MODES`), each `null` when it reads none; the Brain refuses a record whose description states
another than Team read, or a schedule the person did not give, except a replacement's own listed schedule or output. A
new Routine's output the person has not chosen yet is not checked: Team asks for it before any card.
`routine_settings_answer` is true exactly when the person's send is Admin's composed answer whose answer is one fixed
label Admin offers to Team's schedule, over-budget, or output question (`routine_proposal.SCHEDULE_CHOICES`,
`routine_proposal.OVER_BUDGET_ANSWERS` filled with the question's interval, or `routine_proposal.OUTPUT_CHOICES`, in
any interface language); the Brain is then offered no Action and only records again, keeping the work its record
intent selected. Any other answer, typed, mixed with other work, or to a target or rerun question, keeps every
Action. Team sends these fields together only as its recording makes them, and the Brain refuses any other
combination: every field about a recording comes only beside the Routine listing, which never comes with attachments
(only a send without files records); `routine_question` and `routine_schedule` come only with `routine_mode` true,
since a pending question or a stated schedule makes the chat about a Routine, and so a settings answer, which answers a
pending settings question, does too; `routine_rerun` comes only beside a `routine-work-rerun` `routine_question`.
`routine_output` may come outside Routine mode. `routine_proposal` is
the Routine's confirmation card (`routine_proposal.canonical_proposal`), at most 160 KiB, which Team checks against the whole
terminal line bound before publishing: exactly `{proposal_id, expires_at, replaces, name, description, steps}` (ADR-0101
amendment, 2026-10-15). `replaces` is `null` for a new Routine or the id of the Routine it changes; `description` is the
recording agent's one plain paragraph, which states how often the Routine runs exactly as Team read it from the person
and what is done with each run's result; the schedule, zone, output, and permitted Actions are Team's own, bound by the
confirmation, never carried on the card. Each step is `{position,
assistant, action, read_only, inputs}`, and each input `{member, origin, value, step, pointer, where, item}` names
exactly where its value comes from: `request` (named in the person's request) or `assistant` (chosen by the assistant,
the same on every run), each with the literal's complete JSON text escaped (never cut); `clock`, the date of each run
(`value` `null`) or a text whose dates each run writes for its own date (`value` the complete template as a person
reads it, escaped: every literal brace doubled and each date in braces as its format, then after `|` its month-name
language and its offset in days, such as `{D month YYYY|pt}` or `{DD/MM/YYYY|+6}`);
`step`, an earlier step's position and RFC 6901 pointer; or `selector`, the same through the one array item whose
`where` member (`{member, value_json}`, the constant's JSON text as `routine.where_text` escapes it) matches, then that
item's own pointer. A Supervisor answers the card once: `POST /v1/teams/:team_id/routines/proposals/:proposal_id` with `{}`
confirms it, which creates or changes the Routine, and `DELETE` on the same path revokes the card; both answer
`routine_proposal.canonical_proposal_answer`, `{team_id, proposal_id, routine_id, status}` with status `created`, `changed`, or
`revoked` (whose `routine_id` is `null`). A revoked or already-consumed card is answered as such, never twice applied.

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

A Team Routine (ADR-0086) fires on a closed schedule (`routine.canonical_schedule`): `daily` at `HH:MM`, `weekly` on a
weekday (0 is Monday) at `HH:MM`, or `monthly` on day 1 to 28 at `HH:MM`, in an IANA timezone name
(`routine.canonical_timezone`; Team also requires that the zone loads), or `continuous` (any interval the person
states, hours included): exactly `{kind, gap}`, its first run right after confirmation and each next one due `gap`
seconds (5 to 31,622,400, 366 days) after the previous one started, never overlapping. Its rolling 24-hour cap is
derived, never carried: `routine.continuous_cap(gap)`, `ceil(86400 / gap)` (at most `routine.MAX_CONTINUOUS_CAP`,
17,280), so the interval a person stated runs all day and no cap ever rewrites it (ADR-0101).
`routine.daily_cap` is a schedule's rolling 24-hour cap, that cap for a continuous one and one otherwise; the Team's
daily Action-step budget alone bounds what its Routines' caps may start together. A Routine is recorded from the work the ordinary chat agent did in one turn and exists only once a person
confirms its card (ADR-0101). Its notice then has the Routine outcome `created` or `changed`, no run id, and the empty
detail `{}`: the notice's own `name` says which Routine. A Routine view (`routine_notice.canonical_routine_view`) carries
its name (`routine.canonical_name`, 1 to 80 NFC printable characters on one line), its revision's summary, its output
disposition (`routine.canonical_disposition`: `{mode, step}`, where `mode` is `show`, `changes`, or `none`, and `step`
is the 1-based position of the shown step for `show` and `changes`), its schedule, zone, and the zone's source
(`routine.zoned`: `browser`, `person`, a zone the person wrote, or `none`, when the Routine runs on `UTC` by convention,
its run date included, which is never a claim about the person), its next run, and its `state` (`active` or `paused`). A plan holds 1 to 256 steps; on the
wire a step is always named by its position, never by its internal id. The summary
(`routine.canonical_summary`) is `{revision, plan_digest, steps, actions, more}`: the step count and the Actions as runs
of consecutive equal `[assistant, action, count]`, at most 16 runs, with `more` counting the steps after them, within
`routine_notice.MAX_SUMMARY_BYTES`. The steps themselves are read page by page: `GET
/v1/teams/:team_id/routines/:routine_id/revisions/:revision/steps/:offset` answers `routine.canonical_page`, `{routine_id,
revision, plan_digest, total, offset, steps, next}`, at most 64 whole consecutive steps in at most 96 KiB, and refuses a revision that is no longer current (`routine-revision-changed`), so a reader
never combines two revisions. A projected step (`routine.canonical_step`) is exactly `{position, assistant, action,
read_only, inputs, stored_inputs}`, at most `routine.MAX_STEP_VIEW_BYTES` (24 KiB; Team refuses a plan with a larger
step, never truncating it): each input, sorted by member, is a `literal` whose `value` is `routine.literal_preview` of
its JSON (at most 120 characters, every control or invisible character escaped), a `run_clock` whose `value` is `date`
or `routine.preview` of a dated text's template as the card shows it,
or a `step_output` naming an earlier step by position and an RFC 6901 pointer, and, when it selects through an array
item, its `where` (`{member, value_json}`) and that item's `item` pointer (otherwise both `null`); `stored_inputs` names
the Stored Inputs the step's Action uses by id only, never a value. The Routine view a Supervisor lists
(`routine_notice.canonical_routine_view`) carries the same name, summary, disposition, schedule, zone and its source, and
standing scope. `GET /v1/teams/:team_id/routines` answers the Team's whole list, every Routine, live run, and
unresolved incident, within `routine_notice.MAX_ROUTINE_LIST_BYTES`; it is the only Team answer above the Local API's 128 KiB
response cap.

Every notice (`routine_notice.canonical_notice`) names its Routine by the `name` it had when Team wrote that version, so a row
keeps its title after the Routine is renamed or deleted, and carries `usage` and `protection_lost`. A run notice's
`usage` (`routine.canonical_run_usage`) is `{duration_ms}`, its active time excluding frozen time; a Routine run asks
no model, so it reports no tokens. A Routine outcome carries `usage` `null`, except `healthy`, which carries its runs'
summed duration.
`protection_lost` is true on a run's notice versions written after the run lost the protection of its secret values
(ADR-0101), so nothing it produced afterwards was shown anywhere; it is always false on a Routine outcome.
The Routine outcome `deleted` (detail `{}`) closes a Routine's timeline once its confirmed deletion completes.
The Routine outcome `streak-paused` (detail `{failures}`, 1 to `routine.MAX_FAILURE_STREAK`) says its consecutive
failed runs paused it, in the write that ended the run whose failure did.

A run has one notice, keyed by its run id, whose version grows as the run goes on (`routine_notice.canonical_notice_detail`
closes each outcome's detail). `done` and `recovered` carry `{plan, output}`: the `plan` summary of the
revision they carried out, never an input, and their `output`; `recovered` is a run that a
continuation completed after a hold. `output` is `null` unless the run shows a step's result: then it is
`routine.canonical_output`, `{step, state, value, truncated}`, with `state` `shown` and `value` Team's bounded, redacted
projection of that step's validated result, or `unchanged` or `unavailable` with `value` `null`. A projection node is
one closed variant: `{kind: null}`, `{kind: bool, value}`, `{kind: number, value}` (its exact JSON number text, at most 64 characters, so no consumer rounds it; a longer number is shown as text), `{kind: text, value, cut}`
(at most 300 characters, every control or invisible character escaped), `{kind: redacted}`, `{kind: elided}` (past the
depth bound), `{kind: list, items, omitted}` (at most 50 items), or `{kind: fields, fields, omitted}` (at most 24 distinct
`[label, node]` pairs, each label at most 64 characters), with containers nested at most four deep and the whole output
at most `routine.MAX_OUTPUT_BYTES` (16 KiB); `cut`, `omitted`, `elided`, and `truncated` mark every cut. A `changes`
Routine publishes a completed run only when its result differs from the last one shown, and `none` publishes no
completion of its own; a run that already has a notice always gets its terminal version.

Every call is placed by its position (`routine.canonical_position`): its step's 1-based position, an integer, among
the plan's `steps`. A notice's detail is exactly what Admin keeps of it, so no notice names an Action: a held run's call
is its incident's, and what a run did is its step records'. `held` carries `{}`; a `frozen` run carries its
`request_kind`: `human` or `integrations`. The same run's notice then goes on as `paused` with a `reason`
(`exhausted`, `policy`, or `evidence`, recovery evidence Team never classified), or `user-skipped` (`{}`) when a person
set the run aside, by Rodar or by deleting its Routine. A
person's `user-skipped` is a run outcome; the Routine outcome `skipped` reports missed firings and has no run id. A
continuous Routine's healthy runs that show nothing, each completed with no earlier notice, share one versioned
`healthy` Routine notice per minute bucket instead: its instant is the minute's start and its `runs`, at most
`routine.MAX_ROLLUP_RUNS`, counts them and is also its version. Every other outcome stays one notice per run. The rollup
minute only moves forward: a run whose clock fell back into an earlier minute keeps its own notice. A Routine change
keeps its minute's count and usage, and the `routine_rollup_delivery` vectors pin exact delivery sequences, with the
transcript rows Admin must end with. `failed` names its `code`, and `denied` and `stopped` carry `{}`; a run whose
failed call may have acted is held instead. The Routine outcome `scope-changed` (`{}`) says its Assistants no longer
match its pins.

What a run did step by step is read page by page from `GET
/v1/teams/:team_id/routines/runs/:run_id/steps/:snapshot/:offset` (`routine_run.canonical_run_steps`), bound to the run's own
revision (`routine_id`, `revision`, `plan_digest`, and `total`, its plan's step count) and to one `snapshot` of its
retained records (`latest` asks for the current one; a page naming a snapshot whose records changed since is refused
`routine-run-changed`). Each entry
(`routine_run.canonical_run_step`) has its `position` and is `done`, `recovered` (a verified occurrence, with no duration of
its own), `failed` (its attempt failed; the run's notice says whether it was held), `stopped` (Stop or the run's
deadline cut the attempt, which says nothing about whether it acted), or `waiting` (frozen for a person), with its Assistant Action, attempt, `duration_ms`, and the inputs that attempt
was given, each `{member, value}` with a redacted preview (`null` when its source's secrecy cannot be established). A
step with no record is `not_run` only when the run's terminal record proves it never started, and `unavailable`
otherwise. The page is
self-contained and never needs the revision's plan, so it renders after any later change. Records expire after seven
days. Each per-attempt diagnostic also names its `position`. Admin's claim is exactly `{long}`, whether it can take a long
run now; a claimed run carries its `active_seconds` budget, which grows with its revision's steps
(`routine_run.active_seconds`) and makes it long past 600 seconds.

A Supervisor's `GET /v1/teams/:team_id/routines` lists each Routine (`routine_notice.canonical_routine_view`), its live runs
(`routine_notice.canonical_run_view`: its id, Routine, and status, and for a frozen one the request it waits for and its
call), and its unresolved `incidents`, at most `routine_notice.MAX_UNRESOLVED_INCIDENTS`
(`routine_notice.canonical_incident_view`): each held run's id, Routine, and the call it was held at by `position` of
`steps`. `POST /v1/teams/:team_id/routines/incidents/:run_id/card` with
`{}` opens that run's recovery card (`routine_run.canonical_card`): the call it stopped at by `position` of `steps` in the
plan the run executed, the `evidence` of its failure (`recorded`, with the held operation's latest
sanitized `diagnostic` of the same call; `absent` when none is kept; or `unavailable` when it could not be read), and its
`tag`, 64 hex: an entity tag over exactly the state its answer must still find (the Team incarnation, the run, its
Routine and the revision it executed, its held generation and operation, and the Routine's current revision). Rodar and
Excluir are the card's two choices, none recommended. `POST .../answer` with exactly `{tag}`
(`routine_run.canonical_card_answer_request`) answers it with Rodar, as an HTTP If-Match names the entity it expects;
Excluir is never a card answer but the Routine's own confirmed deletion. Rodar sets the held run aside without verifying
it and requests one fresh run of the current revision, answering `{team_id, run_id}`
(`routine_run.canonical_card_answer`); it carries no model credential. It refuses while the held attempt's workload is not proven stopped
(`routine-workload-unquiesced`), while another run of the Routine is live (`routine-busy`), once it is deleted
(`routine-not-found`), or when the Routine's Assistant contracts changed (`routine-contracts-changed`). Anything refused
changes nothing. A tag that no longer matches is `routine-card-stale`, and a run no longer held is
`routine-incident-unavailable`; every answer is checked and applied in the Team's execution slot, and its write checks
the same state again. `POST /v1/teams/:team_id/routines/:routine_id/pause` with `{}` turns a Routine's dispatch off,
while a run already going finishes; `POST /v1/teams/:team_id/routines/:routine_id/resume` with `{}` turns dispatch back
on and starts a fresh failure streak; an unresolved incident still holds the Routine until its card settles it. Deleting
a Routine sets every one of its unresolved incidents aside.

A Local Supervisor reads one Routine run's execution details (ADR-0092) with `GET
/v1/teams/:team_id/routines/runs/:run_id/diagnostics`, answered by `routine_run.canonical_diagnostics`: the Team and run ids
and at most 32 diagnostics, oldest first, one per attempt of one logical operation (`operation_id`, the version 4 UUID
Team journaled, and `attempt` from 1 to 64), each naming its Assistant Action, `position`, and recording instant. Each holds exactly
one of a `failure`, the Team-sanitized handled failure (`error_type`, `message`, `provider`, `http_status`,
`response_excerpt`, and the `redacted` and `truncated` flags, with the Assistant Spec bounds), or a `condition`, the
safe transport condition (`exit-status:<code>`, `stderr-output`, `timeout`, `frame-invalid`, `exit-unavailable`, or
`transport-failed`); raw child output is never reflected. After a run lost its protection, a failure keeps only its
`http_status`: `error_type` is `withheld`, `message` empty, `provider` and `response_excerpt` `null`, and `redacted` true. Text is literal evidence that Admin renders escaped, never as
Markdown or HTML, and a diagnostic is never effect proof or authority. Team keeps these bodies encrypted for at most
seven days and 24 MiB per Team, readable only by the same Team incarnation; deleting the Routine or the Team removes
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
page disposal, or consumption, and never writes it to browser storage.

The exact `POST /v1/teams/:team_id/chat` body carries `message`, `files`, `assistant_ids`, `conversation`,
`locale`, `request`, and `timezone` (`payload.CHAT_BODY_FIELDS`). `assistant_ids` names at most
`payload.MAX_CHAT_ASSISTANTS` Assistants, which equals `payload.MAX_TEAM_ASSISTANTS`, the most Assistants one Team may
have installed. `locale` is one closed interface language (`ar`, `de`, `en`, `es`, `fr`, `ja`, `pt`, `zh`;
`payload.canonical_locale`) or `null`: Local Admin sends the language selected in its interface, and Routine runs
use `null`. Team forwards it only to the Brain's turn start, which pins it for the whole logical
turn and writes replies and clarifications in it; `null` keeps the language of the message (ADR-0090).
`conversation` is one window of committed presentation history strictly before this turn, projected server-side
by Local Admin with the intent-route bounds: at most 8 entries of exactly `{role, text, truncated}` where `role` is
`user` or `assistant`, each text 1 to 512 NFC printable characters with middle truncation, and at most 4,096
characters in total. It is untrusted evidence, never an instruction, fact guarantee, or Action authorization. Team
forwards it only to the Brain's turn start; the Brain uses it only when it retains no completed exchange of its own.
`request` (ADR-0092) is the identity Local Admin mints for every send
(`payload.canonical_request_identity`): `issued_at`, a whole UTC epoch second, and `nonce`, 32 lowercase hex. The
browser never supplies, seals, or replays it. Team validates its exact shape and refuses an invalid one; it does not
deduplicate ordinary sends by it. Only a Supervisor's send without files whose identity is fresh, `issued_at` less
than 900 seconds old and at most 60 seconds ahead of Team's clock (`payload.request_identity_fresh`, exclusive at
900 s), may open or extend the Team's Routine recording span (ADR-0101); any other send ends that span. The identity
never commits a Routine: only the Supervisor's confirmation of a recorded-plan card does. `timezone` is the browser's IANA zone name (`routine.canonical_timezone`) or `null`; Team uses it only as the
zone of a Routine the message records when the person writes no zone of their own (ADR-0101).

A Routine run (ADR-0086) is started by a separate Local Routine identity, never a human Supervisor assertion. Its
Ed25519 assertion travels in `X-Shimpz-Routine` with the JWT key id `local-routine-v1` and the audience
`team-local-routine`; `supervisor.canonical_claims(value, audience=ROUTINE_AUDIENCE)` admits the same request, body,
lifetime, and one-use nonce bindings as a Supervisor assertion, requires `authority: "routine"` with
`authority_sha256` equal to the SHA-256 of the run's lease token, and refuses any model, human assurance, or decision
binding.
Admin's scheduler claims under the Team bearer with `POST /v1/routines/claim` and exactly `{long}`
(`routine_run.canonical_claim_request`): no model configuration or key gates a claim, because a healthy replayed run
needs none (ADR-0092). The answer (`routine_run.canonical_claim`) is one run with its lease
token and the Routine `revision` and `plan_digest` it was claimed at, or `null` with `next_due_at`, the
earliest epoch second a Routine of a Team Admin can run becomes due (`null` when none will), so Admin wakes then while
still reconciling on its own interval. The run's signed segment request, `POST
/v1/teams/:team_id/routines/runs/:run_id/segment`, carries exactly that `{revision, plan_digest}`
(`routine_run.canonical_segment_request`); any other is refused as `routine-revision-stale` before anything runs. The
segment, and a person's answer that resumes a frozen run, answers `{team_id, run_id, status}` with one of
`routine_run.SEGMENT_STATUSES` (`routine_run.canonical_segment_answer`); Admin also requires its own Team and run there.
Admin acknowledges delivered notices in requests of at most
`routine_run.MAX_ACK_DELIVERIES` deliveries. No
run, recovery, or frozen run's answer asks a model, so none carries a model credential (ADR-0092 amendment,
2026-10-15).

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
A classification sent with the decision key may also carry `installable`: one to eight `{name, summary}` entries
of at most 80 code points each, the names and summaries of exactly the installable Assistants the capability plan
for the same message considers (names may repeat). Team forwards them only to Brain's intent route, and its answer
then carries `capability`, `not-needed` only for a confident ordinary task Jev called conversation, else `possible`
(ADR-0077 amendment); without `installable` the answer has no `capability` field. The intent route and the
capability plan bodies are bounded at their JSON worst case (`turn.py`), so any admissible chat message is routed
and planned.

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

An authenticated Supervisor may read one Team file's preview from `GET /v1/teams/:team_id/files/:file_id/preview`
(ADR-0093), under the machine bearer and a request-bound `session` assertion, never the bearer alone. It answers only
for a file of that Team that its listing still shows (an unreferenced file idle past its grace period is collected),
and the read never renews the file's retention. The preview is the one sanitized rendition a chat turn would send the
model, never the original: Team determines the kind from the bytes, never reads an original beyond the 8 MiB image
ceiling, and decodes it only in the networkless preparation helper, so only a JPEG, PNG, or static WebP within the
image limits has one, re-encoded from fresh pixels without metadata as a JPEG or PNG of at most a 1,568-pixel long
edge, 1.2 megapixels, and 512 KiB (`preview.canonical_framing`). The response carries exactly that `Content-Type`,
its exact `Content-Length`, `nosniff`, and `no-store`, and its bytes start with that type's signature. A missing or
collected file answers `404` `file-not-found`; a file without a preview (not such an image,
animated, too large, or unreadable) `422` `file-preview-unsupported`; and a helper that cannot run `503`
`file-preview-unavailable`. Team prepares a preview on demand, keeps no derivative, and admits it into its one
serialized preparation slot within a bounded queue and deadline; beyond them it answers `503` `file-preview-busy` with
`retry_after_ms`. Its Team's teardown and the Space reset end every preview of it, queued or running, and remove its
helper before they continue.

An installed Assistant's summary follows the interface language (ADR-0091). Admin reads it at
`GET /v1/teams/:team_id/assistants/:assistant_id/summary/:locale`, where `locale` is one closed interface language,
and Team answers the same closed `{locale, summary}` as a staged snapshot's summary
(`payload.canonical_snapshot_summary`): the current binding's English catalog summary for `en`, otherwise that one
message's translation from the pack verified against the binding's `pack_digest`. No request copy, catalog, or pack is
ever returned. A missing binding fails as absent, and a missing or mismatched pack fails closed. Admin refuses an answer
whose `locale` is not the one it asked for.

An installed Assistant's page reads the same way at `GET /v1/teams/:team_id/assistants/:assistant_id/details/:locale`
and Team answers the same closed Assistant details object plus `trace_id`, from the exact current
binding: its admitted name, declared Creators (a published resolution's creators, a Local record's declared ones),
description, links, Actions, Integrations, and Stored Input labels, help texts, and help links, localized from the pack verified against the
binding's `pack_digest`. A missing binding fails as absent, a missing or mismatched pack fails closed, and Team
validates its own answer before sending it.

The internal Team bearer is machine authority only for the one-use OAuth callback continuation, the Local bootstrap
reset, health and activity reads, and Admin's Routine scheduler (claim, notices, and their acknowledgement). The bootstrap reset is admitted only while Team independently verifies
that the Supervisor key directory is safe and the Supervisor public key is absent; after identity
establishment it fails closed and never substitutes for human Supervisor evidence. Admin emits one short-lived
Ed25519 assertion in `X-Shimpz-Supervisor` after validating either its current browser session or the exact
password-and-host-capability reset authority. Its `authority` claim distinguishes `session` from `host-reset`, and
Team admits `host-reset` only on exact Space reset. Team binds the assertion to the canonical request and consumes
it once while retaining an independent machine bearer.
For an authentication-gated Action response, that same signed, one-use assertion may carry one
`assurance` binding containing only the exact reviewed `auth:*` kind and pending challenge ID.
Team requires that binding for the matching authentication challenge and rejects it on every
non-authentication request. Credential and factor material never cross this protocol.

Team verifies every Supervisor assertion under its own pin of the Supervisor key, kept in Team's private state: the
first verification pins the key Admin publishes, and later changes to Admin's published file do not change it. The
Supervisor rotates the key with `POST /v1/space/supervisor-key`, a `session` assertion signed by the pinned key whose
body is exactly `{"public_key": key}` (`supervisor.canonical_key_rotation`): the new Ed25519 verification key as the
canonical unpadded base64url of its 32 raw bytes. Team atomically and durably replaces its pin and answers
`{"rotated": true, "key_sha256": digest}` with the lowercase SHA-256 of the new key's raw bytes; from then on an
assertion signed by the earlier key is refused. A retry of a rotation that already took effect, signed by either key,
answers the same; a rotation verified by a key that is no longer pinned, or whose key is not a valid Ed25519 point,
is refused with `409` `supervisor-key-rotation-refused`. Admin keeps the pending new key beside the current one until
Team answers, and after a restart with a pending key it retries signed by the earlier key and, when Team refuses it
because it already switched, signed by the new one. A bootstrap reset, admitted only while Admin publishes no key,
also removes Team's pin.

An authenticated Supervisor may inspect persistent Action input status through
`GET /v1/teams/:team_id/assistant-stored-inputs`. The response is metadata-only: each current
declaration carries exactly `assistant_id`, `stored_input_id`, and `status`; values and generations
never cross HTTP. `DELETE /v1/teams/:team_id/assistant-stored-inputs/:assistant_id/:stored_input_id`
clears only that exact currently declared slot and is idempotent when its value is already absent.
The next Action that needs the slot requests it just in time through the existing human-response
surface. A submitted password is memory-only until the exact Action returns a valid terminal result;
Team then encrypts it for later invocations.

A Local Team has a display name distinct from its immutable id (ADR-0088). `PATCH /v1/teams/:team_id` with
exactly `{"team_name"}` renames it under a Supervisor session and returns exactly `{"team_id", "team_name"}`; a
Local `DELETE /v1/teams/:team_id` carries exactly `{"team_name"}`, the current name, which Team confirms before any
side effect. `payload.canonical_local_team_name` admits a Local display name: the shared 1 to 80 trimmed characters
without controls, already NFC. Supervisor assertions admit `PATCH` alongside `DELETE`, `GET`, `POST`, and `PUT`.
A Local `GET /v1/teams` lists every Team newest first by its Team network's creation instant, compared at Docker's
full nanosecond precision after normalizing the reported offset; only Teams created at the same instant fall back to
ascending `team_id`. Each item keeps exactly `{"team_id", "team_name", "status"}`, and creation metadata that is not
a valid RFC 3339 instant refuses the listing with `503` `team-metadata-invalid`.

`vectors.json` contains positive and negative cases that Team and every consumer mirror execute
independently. Generated consumer mirrors pin the producing Teams commit, verify
`contract-files.sha256`, and remain byte-identical to this directory.

Validate the authority from this directory:

```console
python verify.py
```
