//! Reading one decrypted continuation back, authenticating every structural binding again.

use std::sync::LazyLock;

use assistant_protocol::catalog::message_error;
use strict_json::{Number, Value};
use team_action::challenges::{HumanRequirement, RequestCopy};
use team_action::human::{
    AUTHORIZATION_KINDS, ActionTranscript, HumanResponse, MAX_REQUESTS_PER_ACTION,
    MAX_REQUESTS_PER_TURN, validate_request,
};
use team_chat::orchestrator::{
    ChatContinuation, InvokedAction, MAX_FILE_ACTIONS_PER_TURN, MAX_RESUMABLE_ROUNDS,
};
use team_inference::config::{chat_models, normalize};
use team_inference::memory::canonical_source;
use team_inference::runtime::parse_turn;
use team_inference::usage::TurnUsage;
use team_integrations::pending::{IntegrationRequirement, RequiredIntegration};
use team_protocol::challenge::{
    canonical_file_disclosure, canonical_input_projection, canonical_rendered,
};
use team_protocol::identifiers::{
    canonical_action_id, canonical_assistant_id, canonical_identifier,
};
use team_protocol::payload::{
    FILE_ID_RE, MAX_CHAT_ASSISTANTS, SHA256_RE, canonical_filename, canonical_help_url,
    canonical_locale, canonical_pack_digest, canonical_stored_input_help, canonical_team_name,
    canonical_turn_usage,
};
use team_protocol::purpose::canonical_purpose;
use team_protocol::python::regex::{Pattern, declared};
use team_protocol::python::text;
use team_protocol::python::value::{int, object_of, string};
use team_protocol::turn::{MAX_ACTION_REQUESTS, SAFE_ID_RE};

use super::{
    CodecError, Decoded, IMAGE_RE, MAX_IDENTITY_FILES, MAX_INVOKED_ACTIONS, MAX_STARTED_MS,
    SCHEMA_VERSION,
};
use crate::chat::pending::{Identity, IdentityAssistant, PendingLocalChat, Requirements};

static NETWORK_ID_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[^\s\x00-\x1f\x7f]{1,256}\Z"));
static CONTAINER_ID_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z"));
static RECORDING_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{32}\Z"));

type Result<T> = std::result::Result<T, CodecError>;

fn mapping<'v>(value: &'v Value, fields: &[&str], label: &'static str) -> Result<&'v Value> {
    match value {
        Value::Object(members)
            if members.len() == fields.len()
                && members
                    .iter()
                    .all(|(name, _)| fields.contains(&name.as_str())) =>
        {
            Ok(value)
        }
        _ => Err(CodecError(label)),
    }
}

fn field<'v>(value: &'v Value, name: &str) -> &'v Value {
    value.get(name).unwrap_or(&Value::Null)
}

fn sequence<'v>(value: &'v Value, maximum: usize, label: &'static str) -> Result<&'v [Value]> {
    match value {
        Value::Array(items) if items.len() <= maximum => Ok(items),
        _ => Err(CodecError(label)),
    }
}

/// Bounded printable text: non-empty, unpadded, at most `maximum` characters.
fn bounded_text(value: &Value, maximum: usize, label: &'static str) -> Result<String> {
    match value {
        Value::String(text)
            if !text.is_empty()
                && text::strip(text) == text
                && text::len(text) <= maximum
                && text::isprintable(text) =>
        {
            Ok(text.clone())
        }
        _ => Err(CodecError(label)),
    }
}

fn component(
    value: &Value,
    canonical: fn(&Value) -> Option<&str>,
    label: &'static str,
) -> Result<String> {
    canonical(value).map(str::to_owned).ok_or(CodecError(label))
}

fn interrupt(value: &Value) -> Result<String> {
    string(value)
        .filter(|id| SAFE_ID_RE.is_full(id))
        .map(str::to_owned)
        .ok_or(CodecError("continuation interrupt is malformed"))
}

fn exact_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(Number::Int(number)) => Some(*number),
        _ => None,
    }
}

fn bounded_count(value: &Value, maximum: usize, label: &'static str) -> Result<usize> {
    exact_int(value)
        .and_then(|number| usize::try_from(number).ok())
        .filter(|number| *number <= maximum)
        .ok_or(CodecError(label))
}

fn action_request(value: &Value) -> Result<Value> {
    let raw = mapping(
        value,
        &["interrupt_id", "assistant_id", "action", "input"],
        "continuation Action request",
    )?;
    if !matches!(field(raw, "input"), Value::Object(_)) {
        return Err(CodecError("continuation Action input is malformed"));
    }
    Ok(object_of(vec![
        (
            "interrupt_id",
            Value::String(interrupt(field(raw, "interrupt_id"))?),
        ),
        (
            "assistant_id",
            Value::String(component(
                field(raw, "assistant_id"),
                canonical_assistant_id,
                "continuation Action Assistant",
            )?),
        ),
        (
            "action",
            Value::String(component(
                field(raw, "action"),
                canonical_action_id,
                "continuation Action",
            )?),
        ),
        ("input", field(raw, "input").clone()),
    ]))
}

fn invoked(value: &Value) -> Result<Vec<InvokedAction>> {
    sequence(value, MAX_INVOKED_ACTIONS, "invoked Actions")?
        .iter()
        .map(|item| {
            let entry = mapping(item, &["assistant_id", "action"], "invoked Action")?;
            Ok(InvokedAction {
                assistant_id: component(
                    field(entry, "assistant_id"),
                    canonical_assistant_id,
                    "invoked Action Assistant",
                )?,
                action: component(
                    field(entry, "action"),
                    canonical_action_id,
                    "invoked Action",
                )?,
            })
        })
        .collect()
}

const CONTINUATION_FIELDS: [&str; 5] = [
    "turn",
    "seen_interrupts",
    "invoked",
    "round_index",
    "file_actions",
];

/// The paused turn, admitted as the Brain's own answer would be: it waits on Actions, and a Browser task never pauses
/// (ADR-0117).
fn paused_turn(value: &Value) -> Result<team_inference::runtime::RuntimeTurn> {
    let turn = mapping(value, &["status", "reply", "actions"], "Brain turn")?;
    let actions = sequence(
        field(turn, "actions"),
        MAX_ACTION_REQUESTS,
        "Brain turn Actions",
    )?
    .iter()
    .map(action_request)
    .collect::<Result<Vec<_>>>()?;
    let rebuilt = object_of(vec![
        ("status", field(turn, "status").clone()),
        ("reply", field(turn, "reply").clone()),
        ("clarification", Value::Null),
        ("memory", Value::Array(Vec::new())),
        ("routine", Value::Null),
        ("browser", Value::Null),
        ("actions", Value::Array(actions)),
    ]);
    parse_turn(&rebuilt).ok_or(CodecError("Brain turn is malformed"))
}

fn seen_interrupts(value: &Value) -> Result<Vec<String>> {
    let seen = sequence(value, MAX_ACTION_REQUESTS * 8, "seen Brain interrupts")?
        .iter()
        .map(interrupt)
        .collect::<Result<Vec<_>>>()?;
    let unique = seen
        .iter()
        .enumerate()
        .all(|(index, item)| !seen[..index].contains(item));
    if unique {
        Ok(seen)
    } else {
        Err(CodecError("seen Brain interrupts are malformed"))
    }
}

fn continuation(value: &Value) -> Result<ChatContinuation> {
    let raw = mapping(value, &CONTINUATION_FIELDS, "Brain continuation")?;
    let round_index = bounded_count(
        field(raw, "round_index"),
        MAX_RESUMABLE_ROUNDS - 1,
        "continuation round is malformed",
    )?;
    let file_actions = bounded_count(
        field(raw, "file_actions"),
        MAX_FILE_ACTIONS_PER_TURN,
        "continuation file Actions are malformed",
    )?;
    Ok(ChatContinuation {
        turn: paused_turn(field(raw, "turn"))?,
        seen_interrupts: seen_interrupts(field(raw, "seen_interrupts"))?,
        invoked: invoked(field(raw, "invoked"))?,
        round_index,
        file_actions,
    })
}

fn identity_assistants(value: &Value) -> Result<Vec<IdentityAssistant>> {
    let malformed = CodecError("continuation Assistant identity is malformed");
    let mut assistants: Vec<IdentityAssistant> = Vec::new();
    for item in sequence(value, MAX_CHAT_ASSISTANTS, "continuation Assistants")? {
        let Value::Array(parts) = item else {
            return Err(malformed);
        };
        let [id, image, container] = parts.as_slice() else {
            return Err(malformed);
        };
        let id = canonical_assistant_id(id).ok_or(malformed)?;
        let image = string(image)
            .filter(|image| IMAGE_RE.is_full(image))
            .ok_or(malformed)?;
        let container = string(container)
            .filter(|id| CONTAINER_ID_RE.is_full(id))
            .ok_or(malformed)?;
        if assistants.iter().any(|(known, _, _)| known == id) {
            return Err(malformed);
        }
        assistants.push((id.to_owned(), image.to_owned(), container.to_owned()));
    }
    Ok(assistants)
}

fn identity_file(item: &Value) -> Result<Value> {
    let malformed = CodecError("continuation file is malformed");
    let entry = mapping(
        item,
        &["id", "name", "media_type", "size", "sha256"],
        "continuation file",
    )?;
    let valid = string(field(entry, "id")).is_some_and(|id| FILE_ID_RE.is_full(id))
        && canonical_filename(field(entry, "name")).is_some()
        && string(field(entry, "media_type"))
            .is_some_and(|media| (1..=127).contains(&text::len(media)))
        && exact_int(field(entry, "size")).is_some_and(|size| (0..=MAX_STARTED_MS).contains(&size))
        && string(field(entry, "sha256")).is_some_and(|digest| SHA256_RE.is_full(digest));
    if valid {
        Ok(entry.clone())
    } else {
        Err(malformed)
    }
}

fn identity_files(value: &Value) -> Result<Vec<Value>> {
    let files = sequence(value, MAX_IDENTITY_FILES, "continuation files")?
        .iter()
        .map(identity_file)
        .collect::<Result<Vec<_>>>()?;
    let ids: Vec<&Value> = files.iter().map(|file| field(file, "id")).collect();
    let unique = ids
        .iter()
        .enumerate()
        .all(|(index, id)| !ids[..index].contains(id));
    if unique {
        Ok(files)
    } else {
        Err(CodecError("continuation files are malformed"))
    }
}

fn network_id(value: &Value) -> Result<String> {
    string(value)
        .filter(|id| NETWORK_ID_RE.is_full(id))
        .map(str::to_owned)
        .ok_or(CodecError("continuation network identity is malformed"))
}

fn identity(value: &Value) -> Result<Identity> {
    if let Ok(routine) = mapping(value, &["network_id", "assistants"], "") {
        return Ok(Identity::Routine {
            network_id: network_id(field(routine, "network_id"))?,
            assistants: identity_assistants(field(routine, "assistants"))?,
        });
    }
    let raw = mapping(
        value,
        &[
            "team_name",
            "network_id",
            "assistants",
            "files",
            "inference",
        ],
        "continuation Team identity",
    )?;
    let team_name = canonical_team_name(field(raw, "team_name"))
        .map(str::to_owned)
        .ok_or(CodecError("continuation Team name is malformed"))?;
    let inference = mapping(
        field(raw, "inference"),
        &["provider", "model"],
        "continuation inference",
    )?;
    let config = normalize(field(inference, "provider"), field(inference, "model"))
        .map_err(|_| CodecError("continuation inference is malformed"))?;
    Ok(Identity::Chat {
        team_name,
        network_id: network_id(field(raw, "network_id"))?,
        assistants: identity_assistants(field(raw, "assistants"))?,
        files: identity_files(field(raw, "files"))?,
        inference: (config.provider, config.model),
    })
}

fn usage(value: &Value) -> Result<Option<TurnUsage>> {
    if matches!(value, Value::Null) {
        return Ok(None);
    }
    let malformed = CodecError("pending turn usage is malformed");
    let raw = mapping(value, &["started_ms", "models"], "pending turn usage")?;
    let started_ms = exact_int(field(raw, "started_ms"))
        .filter(|started| (0..=MAX_STARTED_MS).contains(started))
        .ok_or(malformed)?;
    let Value::Array(models) = field(raw, "models") else {
        return Err(malformed);
    };
    if !models.is_empty() {
        let wire = object_of(vec![
            ("duration_ms", int(0)),
            ("models", Value::Array(models.clone())),
        ]);
        canonical_turn_usage(&wire).ok_or(malformed)?;
    }
    let models = models
        .iter()
        .map(|model| {
            let text = |name: &str| {
                string(field(model, name))
                    .map(str::to_owned)
                    .ok_or(malformed)
            };
            let count = |name: &str| exact_int(field(model, name)).ok_or(malformed);
            Ok((
                text("provider")?,
                text("model")?,
                count("input_tokens")?,
                count("output_tokens")?,
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(Some(TurnUsage { started_ms, models }))
}

fn human_response(value: &Value, ordinal: usize) -> Result<HumanResponse> {
    let malformed = CodecError("human response is malformed");
    let raw = mapping(
        value,
        &["kind", "ordinal", "fingerprint", "value"],
        "human response",
    )?;
    let kind = string(field(raw, "kind")).ok_or(malformed)?;
    let fingerprint = string(field(raw, "fingerprint"))
        .filter(|digest| SHA256_RE.is_full(digest))
        .ok_or(malformed)?;
    let position = i64::try_from(ordinal).map_err(|_| malformed)?;
    let answer = field(raw, "value");
    let authorization_unconfirmed =
        AUTHORIZATION_KINDS.contains(&kind) && *answer != Value::Bool(true);
    if kind == "input:password"
        || exact_int(field(raw, "ordinal")) != Some(position)
        || authorization_unconfirmed
    {
        return Err(malformed);
    }
    Ok(HumanResponse {
        kind: kind.to_owned(),
        ordinal,
        fingerprint: fingerprint.to_owned(),
        value: answer.clone(),
    })
}

fn transcripts(value: &Value) -> Result<Vec<ActionTranscript>> {
    let mut kept: Vec<ActionTranscript> = Vec::new();
    let mut count = 0;
    for item in sequence(value, MAX_REQUESTS_PER_TURN, "human transcripts")? {
        let raw = mapping(item, &["interrupt_id", "responses"], "human transcript")?;
        let responses = sequence(
            field(raw, "responses"),
            MAX_REQUESTS_PER_ACTION,
            "human responses",
        )?
        .iter()
        .enumerate()
        .map(|(ordinal, response)| human_response(response, ordinal))
        .collect::<Result<Vec<_>>>()?;
        count += responses.len();
        let interrupt_id = interrupt(field(raw, "interrupt_id"))?;
        if kept
            .iter()
            .any(|transcript| transcript.interrupt_id == interrupt_id)
        {
            return Err(CodecError("human transcripts are malformed"));
        }
        kept.push(ActionTranscript {
            interrupt_id,
            responses,
        });
    }
    if count > MAX_REQUESTS_PER_TURN {
        return Err(CodecError("human transcripts are malformed"));
    }
    Ok(kept)
}

const PENDING_FIELDS: [&str; 13] = [
    "continuation",
    "assistant_ids",
    "file_ids",
    "provider",
    "identity",
    "transcripts",
    "requests_used",
    "locale",
    "usage",
    "paused_batch",
    "recording",
    "model",
    "source",
];

fn pending_ids(raw: &Value) -> Result<(Vec<String>, Vec<String>)> {
    let assistant_ids = sequence(field(raw, "assistant_ids"), 16, "pending Assistants")?
        .iter()
        .map(|item| component(item, canonical_assistant_id, "pending Assistant"))
        .collect::<Result<Vec<_>>>()?;
    let sorted = assistant_ids.windows(2).all(|pair| pair[0] < pair[1]);
    if !sorted {
        return Err(CodecError("pending Assistants are malformed"));
    }
    let file_ids = sequence(field(raw, "file_ids"), MAX_IDENTITY_FILES, "pending files")?
        .iter()
        .map(|item| {
            string(item)
                .filter(|id| FILE_ID_RE.is_full(id))
                .map(str::to_owned)
                .ok_or(CodecError("pending files are malformed"))
        })
        .collect::<Result<Vec<_>>>()?;
    let unique = file_ids
        .iter()
        .enumerate()
        .all(|(index, id)| !file_ids[..index].contains(id));
    if unique {
        Ok((assistant_ids, file_ids))
    } else {
        Err(CodecError("pending files are malformed"))
    }
}

/// A frozen Routine run asks no model, reads no file, and has no author; a chat turn names its Team's provider.
fn pending_provider(
    raw: &Value,
    identity: &Identity,
    file_ids: &[String],
) -> Result<Option<String>> {
    let provider = field(raw, "provider");
    match identity {
        Identity::Routine { .. } => {
            let unnamed = ["usage", "recording", "model", "source"]
                .iter()
                .all(|name| matches!(field(raw, name), Value::Null));
            if matches!(provider, Value::Null) && file_ids.is_empty() && unnamed {
                Ok(None)
            } else {
                Err(CodecError("pending Routine run is malformed"))
            }
        }
        Identity::Chat { inference, .. } => {
            let provider = string(provider)
                .filter(|provider| chat_models(provider).is_some())
                .ok_or(CodecError("pending provider is malformed"))?;
            if inference.0 == provider {
                Ok(Some(provider.to_owned()))
            } else {
                Err(CodecError("pending provider binding is malformed"))
            }
        }
    }
}

fn optional_text(
    value: &Value,
    admit: impl Fn(&str) -> bool,
    label: &'static str,
) -> Result<Option<String>> {
    match value {
        Value::Null => Ok(None),
        Value::String(text) if admit(text) => Ok(Some(text.clone())),
        _ => Err(CodecError(label)),
    }
}

fn locale_text(value: &Value) -> Result<Option<String>> {
    optional_text(
        value,
        |locale| canonical_locale(&Value::String(locale.to_owned())).is_some(),
        "pending locale is malformed",
    )
}

fn source(value: &Value) -> Result<Option<team_inference::memory::Source>> {
    match value {
        Value::Null => Ok(None),
        source => canonical_source(source)
            .map(Some)
            .ok_or(CodecError("pending source is malformed")),
    }
}

/// The paused turn's human answers and their budget, which covers every kept answer.
fn answers(raw: &Value) -> Result<(Vec<ActionTranscript>, usize)> {
    let transcripts = transcripts(field(raw, "transcripts"))?;
    let requests_used = bounded_count(
        field(raw, "requests_used"),
        MAX_REQUESTS_PER_TURN,
        "human request budget is malformed",
    )?;
    let answered: usize = transcripts
        .iter()
        .map(|transcript| transcript.responses.len())
        .sum();
    if answered > requests_used {
        return Err(CodecError("human request budget is malformed"));
    }
    Ok((transcripts, requests_used))
}

fn pending(value: &Value) -> Result<PendingLocalChat> {
    let raw = mapping(value, &PENDING_FIELDS, "pending continuation")?;
    let (assistant_ids, file_ids) = pending_ids(raw)?;
    let identity = identity(field(raw, "identity"))?;
    let provider = pending_provider(raw, &identity, &file_ids)?;
    let (transcripts, requests_used) = answers(raw)?;
    let recording = field(raw, "recording");
    let recording = optional_text(
        recording,
        |id| RECORDING_RE.is_full(id),
        "pending recording is malformed",
    )?;
    let models = provider
        .as_deref()
        .and_then(chat_models)
        .unwrap_or_default();
    let model = optional_text(
        field(raw, "model"),
        |model| models.contains(&model),
        "pending model is malformed",
    )?;
    let batch = "continuation paused Action batch is malformed";
    Ok(PendingLocalChat {
        continuation: continuation(field(raw, "continuation"))?,
        assistant_ids,
        file_ids,
        provider,
        identity,
        transcripts,
        requests_used,
        locale: locale_text(field(raw, "locale"))?,
        usage: usage(field(raw, "usage"))?,
        paused_batch: optional_text(field(raw, "paused_batch"), |_| true, batch)?,
        recording,
        model,
        source: source(field(raw, "source"))?,
    })
}

fn ordered(values: Vec<String>, label: &'static str) -> Result<Vec<String>> {
    let strictly = values.windows(2).all(|pair| pair[0] < pair[1]);
    if values.is_empty() || !strictly {
        return Err(CodecError(label));
    }
    Ok(values)
}

fn required_integration(item: &Value) -> Result<RequiredIntegration> {
    let malformed = CodecError("integration requirement is malformed");
    let Value::Array(parts) = item else {
        return Err(malformed);
    };
    let [id, provider, scopes] = parts.as_slice() else {
        return Err(malformed);
    };
    let scopes = sequence(scopes, 128, "integration scopes")?
        .iter()
        .map(|scope| bounded_text(scope, 128, "integration scopes is malformed"))
        .collect::<Result<Vec<_>>>()?;
    Ok(RequiredIntegration {
        id: component(id, canonical_identifier, "integration id is malformed")?,
        provider: component(
            provider,
            canonical_identifier,
            "integration provider is malformed",
        )?,
        scopes: ordered(scopes, "integration scopes is malformed")?,
    })
}

const INTEGRATION_FIELDS: [&str; 4] = [
    "assistant_id",
    "assistant_name",
    "action_ids",
    "integrations",
];

fn integration_requirement(value: &Value) -> Result<IntegrationRequirement> {
    let raw = mapping(value, &INTEGRATION_FIELDS, "integration requirement")?;
    let integrations = sequence(
        field(raw, "integrations"),
        16,
        "integration requirement integrations",
    )?
    .iter()
    .map(required_integration)
    .collect::<Result<Vec<_>>>()?;
    if integrations.is_empty() {
        return Err(CodecError("integration requirement is malformed"));
    }
    let action_ids = sequence(field(raw, "action_ids"), 128, "integration Actions")?
        .iter()
        .map(|item| {
            component(
                item,
                canonical_action_id,
                "integration Actions is malformed",
            )
        })
        .collect::<Result<Vec<_>>>()?;
    let assistant = field(raw, "assistant_id");
    Ok(IntegrationRequirement {
        assistant_id: component(
            assistant,
            canonical_assistant_id,
            "integration Assistant is malformed",
        )?,
        assistant_name: bounded_text(
            field(raw, "assistant_name"),
            80,
            "integration Assistant name is malformed",
        )?,
        action_ids: ordered(action_ids, "integration Actions is malformed")?,
        integrations,
    })
}

/// Re-admits the paused request against exactly the catalog entries it references (ADR-0091).
fn human_request(value: &Value, messages: &Value) -> Result<team_action::human::HumanRequest> {
    let malformed = CodecError("human requirement request is malformed");
    let kind = value.get("kind").and_then(string).ok_or(malformed)?;
    let Value::Array(entries) = messages else {
        return Err(malformed);
    };
    if entries
        .iter()
        .any(|message| message_error(message).is_some())
    {
        return Err(CodecError("human requirement catalog is malformed"));
    }
    // The record is Team-authenticated and its Stored Input was admitted against the Action declaration at pause.
    let stored: Vec<String> = value
        .get("stored_input")
        .and_then(string)
        .map(str::to_owned)
        .into_iter()
        .collect();
    let request =
        validate_request(value, &[kind.to_owned()], &stored, messages).map_err(|_| malformed)?;
    if request.messages() != *messages {
        return Err(CodecError("human requirement catalog is malformed"));
    }
    Ok(request)
}

fn request_copy(value: &Value, request: &team_action::human::HumanRequest) -> Result<RequestCopy> {
    let raw = mapping(
        value,
        &[
            "locale",
            "catalog_digest",
            "pack_digest",
            "rendered",
            "help",
        ],
        "human request copy",
    )?;
    let help = field(raw, "help");
    let valid = canonical_locale(field(raw, "locale")).is_some()
        && canonical_pack_digest(field(raw, "catalog_digest")).is_some()
        && canonical_pack_digest(field(raw, "pack_digest")).is_some()
        && canonical_rendered(field(raw, "rendered"), &request.payload()).is_some()
        && (matches!(help, Value::Null) || canonical_stored_input_help(help).is_some());
    if !valid {
        return Err(CodecError("human request copy is malformed"));
    }
    let text = |name: &str| string(field(raw, name)).unwrap_or_default().to_owned();
    Ok(RequestCopy {
        locale: text("locale"),
        catalog_digest: text("catalog_digest"),
        pack_digest: text("pack_digest"),
        rendered: field(raw, "rendered").clone(),
        help: string(help).map(str::to_owned),
    })
}

const HUMAN_FIELDS: [&str; 15] = [
    "assistant_id",
    "assistant_name",
    "action_id",
    "action_summary",
    "interrupt_id",
    "request",
    "messages",
    "assistant_version",
    "copy",
    "help_url",
    "help_text",
    "purpose",
    "purpose_locale",
    "file",
    "input",
];

/// Whether a paused request's help, purpose, file, and input presentation are each where they belong.
fn presentation_valid(
    raw: &Value,
    request: &team_action::human::HumanRequest,
    copy: &RequestCopy,
) -> bool {
    let stored = request.kind == "input:password" && request.stored_input.is_some();
    let (help_url, help_text) = (field(raw, "help_url"), field(raw, "help_text"));
    let help = if stored {
        canonical_help_url(help_url).is_some()
            && canonical_stored_input_help(help_text).is_some()
            && copy.help.is_some()
    } else {
        matches!((help_url, help_text), (Value::Null, Value::Null)) && copy.help.is_none()
    };
    let (purpose, purpose_locale) = (field(raw, "purpose"), field(raw, "purpose_locale"));
    let purpose_valid = matches!((purpose, purpose_locale), (Value::Null, Value::Null))
        || (canonical_purpose(purpose).is_some() && canonical_locale(purpose_locale).is_some());
    let authorizes = AUTHORIZATION_KINDS.contains(&request.kind.as_str());
    let file = field(raw, "file");
    let file_valid = matches!(file, Value::Null)
        || (authorizes && canonical_file_disclosure(file).as_ref() == Some(file));
    let shown = field(raw, "input");
    let input_valid =
        matches!(shown, Value::Null) || (authorizes && canonical_input_projection(shown).is_some());
    help && purpose_valid && file_valid && input_valid
}

fn human_requirement(value: &Value) -> Result<HumanRequirement> {
    let raw = mapping(value, &HUMAN_FIELDS, "human requirement")?;
    let request = human_request(field(raw, "request"), field(raw, "messages"))?;
    let copy = request_copy(field(raw, "copy"), &request)?;
    if !presentation_valid(raw, &request, &copy) {
        return Err(CodecError("human requirement presentation is malformed"));
    }
    let optional = |name: &str| string(field(raw, name)).map(str::to_owned);
    let present = |name: &str| match field(raw, name) {
        Value::Null => None,
        value => Some(value.clone()),
    };
    Ok(HumanRequirement {
        assistant_id: component(
            field(raw, "assistant_id"),
            canonical_assistant_id,
            "human Assistant is malformed",
        )?,
        assistant_name: bounded_text(
            field(raw, "assistant_name"),
            80,
            "human Assistant name is malformed",
        )?,
        action_id: component(
            field(raw, "action_id"),
            canonical_action_id,
            "human Action is malformed",
        )?,
        action_summary: bounded_text(
            field(raw, "action_summary"),
            500,
            "human Action summary is malformed",
        )?,
        interrupt_id: interrupt(field(raw, "interrupt_id"))?,
        request,
        assistant_version: bounded_text(
            field(raw, "assistant_version"),
            40,
            "human Assistant version is malformed",
        )?,
        copy,
        help_url: optional("help_url"),
        help_text: optional("help_text"),
        purpose: optional("purpose"),
        purpose_locale: optional("purpose_locale"),
        file: present("file"),
        input: present("input"),
    })
}

fn requirements(kind: &str, value: &Value) -> Result<Requirements> {
    let items = sequence(value, 64, "continuation requirements")?;
    match (kind, items) {
        ("integrations", items) if !items.is_empty() => Ok(Requirements::Integrations(
            items
                .iter()
                .map(integration_requirement)
                .collect::<Result<Vec<_>>>()?,
        )),
        ("human", [item]) => Ok(Requirements::Human(Box::new(human_requirement(item)?))),
        _ => Err(CodecError("stored continuation kind is malformed")),
    }
}

/// Decodes one plaintext of `kind` and authenticates its release `bound` bindings again.
///
/// # Errors
/// [`CodecError`] for anything outside the closed codec or a changed binding.
pub fn decode(kind: &str, payload: &[u8], bound: &[String]) -> Result<Decoded> {
    let body =
        strict_json::parse(payload).map_err(|_| CodecError("continuation is not valid JSON"))?;
    let body = mapping(
        &body,
        &["schema", "kind", "requirements", "pending"],
        "continuation",
    )?;
    if exact_int(field(body, "schema")) != Some(SCHEMA_VERSION)
        || string(field(body, "kind")) != Some(kind)
    {
        return Err(CodecError("stored continuation contract changed"));
    }
    let requirements = requirements(kind, field(body, "requirements"))?;
    let pending = pending(field(body, "pending"))?;
    // A human pause names exactly the Action batch it holds; an Integration pause holds none.
    let batch_valid = match (&requirements, &pending.paused_batch) {
        (Requirements::Integrations(_), None) => true,
        (Requirements::Human(_), Some(batch)) => SHA256_RE.is_full(batch),
        _ => false,
    };
    if !batch_valid {
        return Err(CodecError("continuation paused Action batch is malformed"));
    }
    if super::bindings(&requirements, &pending)? != bound {
        return Err(CodecError("stored continuation release binding changed"));
    }
    Ok(Decoded {
        requirements,
        pending,
    })
}
