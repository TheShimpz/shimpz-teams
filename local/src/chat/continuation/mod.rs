//! The closed JSON codec of an encrypted paused turn (`local/chat/continuation.py`), schema 10.
//!
//! The plaintext is canonical JSON `{"schema","kind","requirements","pending"}`; the store authenticates it with the
//! release bindings it names (`assistant/action/image/fingerprint-or-dash`). Nothing is persisted that a restart
//! cannot restore: an encoding is decoded again before it is kept.

mod decode;

use std::sync::LazyLock;

use strict_json::Value;
use team_action::challenges::HumanRequirement;
use team_action::human::ActionTranscript;
use team_chat::orchestrator::ChatContinuation;
use team_inference::usage::TurnUsage;
use team_integrations::pending::IntegrationRequirement;
use team_protocol::python::json::canonical;
use team_protocol::python::regex::{Pattern, declared};
use team_protocol::python::value::{int, object_of};

pub use self::decode::{checkpoint, decode};
use super::pending::{Identity, PendingLocalChat, Requirements};

/// The codec's schema.
pub const SCHEMA_VERSION: i64 = 10;
/// The most Actions a continuation records as invoked.
pub const MAX_INVOKED_ACTIONS: usize = 512;
/// The most files an identity names.
pub const MAX_IDENTITY_FILES: usize = 8;
/// A turn's wall-clock admission in epoch milliseconds, within the exact JSON integer range.
pub const MAX_STARTED_MS: i64 = (1 << 53) - 1;
/// The chat store's plaintext bound.
pub const MAX_PLAINTEXT_BYTES: usize = 256 * 1024;

static IMAGE_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(r"(?:sha256:[0-9a-f]{64}|[^\s\x00-\x1f\x7f]{1,512}@sha256:[0-9a-f]{64})\Z")
});

/// A continuation violated the closed codec.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CodecError(pub &'static str);

/// One decoded continuation.
#[derive(Clone, Debug, PartialEq)]
pub struct Decoded {
    /// What the turn waits for.
    pub requirements: Requirements,
    /// The paused turn.
    pub pending: PendingLocalChat,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn texts(values: &[String]) -> Value {
    Value::Array(values.iter().map(|value| text(value)).collect())
}

fn optional(value: Option<&String>) -> Value {
    value.map_or(Value::Null, |value| text(value))
}

/// A continuation's payload; `full` keeps every member of its Brain turn, as a durable checkpoint does.
fn continuation_payload(continuation: &ChatContinuation, full: bool) -> Value {
    let turn = &continuation.turn;
    let actions = turn
        .actions
        .iter()
        .map(|request| {
            object_of(vec![
                ("interrupt_id", text(&request.interrupt_id)),
                ("assistant_id", text(&request.assistant_id)),
                ("action", text(&request.action)),
                ("input", request.input.clone()),
            ])
        })
        .collect();
    let invoked = continuation
        .invoked
        .iter()
        .map(|item| {
            object_of(vec![
                ("assistant_id", text(&item.assistant_id)),
                ("action", text(&item.action)),
            ])
        })
        .collect();
    let count = |value: usize| int(i64::try_from(value).unwrap_or(i64::MAX));
    let turn = if full {
        team_inference::runtime::turn_value(turn)
    } else {
        object_of(vec![
            ("status", text(turn.status.name())),
            ("reply", text(&turn.reply)),
            ("actions", Value::Array(actions)),
        ])
    };
    object_of(vec![
        ("turn", turn),
        ("seen_interrupts", texts(&continuation.seen_interrupts)),
        ("invoked", Value::Array(invoked)),
        ("round_index", count(continuation.round_index)),
        ("file_actions", count(continuation.file_actions)),
    ])
}

fn assistants_payload(assistants: &[(String, String, String)]) -> Value {
    Value::Array(
        assistants
            .iter()
            .map(|(id, image, container)| {
                Value::Array(vec![text(id), text(image), text(container)])
            })
            .collect(),
    )
}

fn identity_payload(identity: &Identity) -> Value {
    match identity {
        Identity::Routine {
            network_id,
            assistants,
        } => object_of(vec![
            ("network_id", text(network_id)),
            ("assistants", assistants_payload(assistants)),
        ]),
        Identity::Chat {
            team_name,
            network_id,
            assistants,
            files,
            inference,
        } => object_of(vec![
            ("team_name", text(team_name)),
            ("network_id", text(network_id)),
            ("assistants", assistants_payload(assistants)),
            ("files", Value::Array(files.clone())),
            (
                "inference",
                object_of(vec![
                    ("provider", text(&inference.0)),
                    ("model", text(&inference.1)),
                ]),
            ),
        ]),
    }
}

fn usage_payload(usage: Option<&TurnUsage>) -> Value {
    let Some(usage) = usage else {
        return Value::Null;
    };
    let models = usage
        .models
        .iter()
        .map(|(provider, model, inputs, outputs)| {
            object_of(vec![
                ("provider", text(provider)),
                ("model", text(model)),
                ("input_tokens", int(*inputs)),
                ("output_tokens", int(*outputs)),
            ])
        })
        .collect();
    object_of(vec![
        ("started_ms", int(usage.started_ms)),
        ("models", Value::Array(models)),
    ])
}

fn transcripts_payload(transcripts: &[ActionTranscript]) -> Value {
    Value::Array(
        transcripts
            .iter()
            .map(|transcript| {
                object_of(vec![
                    ("interrupt_id", text(&transcript.interrupt_id)),
                    ("responses", Value::Array(transcript.payloads())),
                ])
            })
            .collect(),
    )
}

/// The `pending` member of a continuation.
#[must_use]
pub fn pending_payload(pending: &PendingLocalChat) -> Value {
    pending_value(pending, false)
}

/// A durable chat turn's checkpoint (ADR-0125): its pending state with the Brain's whole turn, which
/// [`checkpoint`] reads back.
#[must_use]
pub fn checkpoint_payload(pending: &PendingLocalChat) -> Value {
    pending_value(pending, true)
}

fn pending_value(pending: &PendingLocalChat, full: bool) -> Value {
    object_of(vec![
        (
            "continuation",
            continuation_payload(&pending.continuation, full),
        ),
        ("assistant_ids", texts(&pending.assistant_ids)),
        ("file_ids", texts(&pending.file_ids)),
        ("provider", optional(pending.provider.as_ref())),
        ("identity", identity_payload(&pending.identity)),
        ("transcripts", transcripts_payload(&pending.transcripts)),
        (
            "requests_used",
            int(i64::try_from(pending.requests_used).unwrap_or(i64::MAX)),
        ),
        ("locale", optional(pending.locale.as_ref())),
        ("usage", usage_payload(pending.usage.as_ref())),
        ("paused_batch", optional(pending.paused_batch.as_ref())),
        ("recording", optional(pending.recording.as_ref())),
        ("model", optional(pending.model.as_ref())),
        (
            "source",
            pending
                .source
                .as_ref()
                .map_or(Value::Null, team_inference::memory::Source::record),
        ),
    ])
}

fn integration_payload(requirement: &IntegrationRequirement) -> Value {
    let integrations = requirement
        .integrations
        .iter()
        .map(|item| {
            Value::Array(vec![
                text(&item.id),
                text(&item.provider),
                texts(&item.scopes),
            ])
        })
        .collect();
    object_of(vec![
        ("assistant_id", text(&requirement.assistant_id)),
        ("assistant_name", text(&requirement.assistant_name)),
        ("action_ids", texts(&requirement.action_ids)),
        ("integrations", Value::Array(integrations)),
    ])
}

fn human_payload(requirement: &HumanRequirement) -> Value {
    let copy = &requirement.copy;
    object_of(vec![
        ("assistant_id", text(&requirement.assistant_id)),
        ("assistant_name", text(&requirement.assistant_name)),
        ("action_id", text(&requirement.action_id)),
        ("action_summary", text(&requirement.action_summary)),
        ("interrupt_id", text(&requirement.interrupt_id)),
        ("request", requirement.request.payload()),
        ("messages", requirement.request.messages()),
        ("assistant_version", text(&requirement.assistant_version)),
        (
            "copy",
            object_of(vec![
                ("locale", text(&copy.locale)),
                ("catalog_digest", text(&copy.catalog_digest)),
                ("pack_digest", text(&copy.pack_digest)),
                ("rendered", copy.rendered.clone()),
                ("help", optional(copy.help.as_ref())),
            ]),
        ),
        ("help_url", optional(requirement.help_url.as_ref())),
        ("help_text", optional(requirement.help_text.as_ref())),
        ("purpose", optional(requirement.purpose.as_ref())),
        (
            "purpose_locale",
            optional(requirement.purpose_locale.as_ref()),
        ),
        ("file", requirement.file.clone().unwrap_or(Value::Null)),
        ("input", requirement.input.clone().unwrap_or(Value::Null)),
    ])
}

fn requirements_payload(requirements: &Requirements) -> Value {
    match requirements {
        Requirements::Integrations(items) => {
            Value::Array(items.iter().map(integration_payload).collect())
        }
        Requirements::Human(requirement) => Value::Array(vec![human_payload(requirement)]),
    }
}

/// The sorted release bindings a continuation is authenticated with.
///
/// # Errors
/// [`CodecError`] when a requirement names an Assistant its identity does not bind.
pub fn bindings(
    requirements: &Requirements,
    pending: &PendingLocalChat,
) -> Result<Vec<String>, CodecError> {
    let malformed = CodecError("continuation release binding is malformed");
    let image = |assistant: &str| {
        pending
            .identity
            .assistants()
            .iter()
            .find(|(id, _, _)| id == assistant)
            .map(|(_, image, _)| image.clone())
            .filter(|image| IMAGE_RE.is_full(image))
            .ok_or(malformed)
    };
    let mut bound = Vec::new();
    match requirements {
        Requirements::Integrations(items) => {
            for requirement in items {
                let image = image(&requirement.assistant_id)?;
                for action in &requirement.action_ids {
                    bound.push(format!("{}/{action}/{image}/-", requirement.assistant_id));
                }
            }
        }
        Requirements::Human(requirement) => {
            let image = image(&requirement.assistant_id)?;
            let fingerprint = &requirement.request.fingerprint;
            bound.push(format!(
                "{}/{}/{image}/{fingerprint}",
                requirement.assistant_id, requirement.action_id
            ));
        }
    }
    bound.sort();
    bound.dedup();
    Ok(bound)
}

fn body(requirements: &Requirements, pending: &PendingLocalChat) -> Vec<u8> {
    canonical(&object_of(vec![
        ("schema", int(SCHEMA_VERSION)),
        ("kind", text(requirements.kind())),
        ("requirements", requirements_payload(requirements)),
        ("pending", pending_payload(pending)),
    ]))
}

/// Encodes one plaintext and its release bindings within `limit` bytes, refusing what would not round-trip: the
/// decoded continuation must encode to exactly the same bytes (JSON objects compare by members, not order).
///
/// # Errors
/// [`CodecError`] for an oversized, unbindable, or non-round-tripping continuation.
pub fn encode(
    requirements: &Requirements,
    pending: &PendingLocalChat,
    limit: usize,
) -> Result<(Vec<String>, Vec<u8>), CodecError> {
    let payload = body(requirements, pending);
    if payload.is_empty() || payload.len() > limit {
        return Err(CodecError("continuation exceeds its fixed byte limit"));
    }
    let bound = bindings(requirements, pending)?;
    // Never persist what a restart cannot restore: an undecodable record would stop Local Team at startup.
    let decoded = decode(requirements.kind(), &payload, &bound)?;
    if body(&decoded.requirements, &decoded.pending) != payload {
        return Err(CodecError("continuation does not round-trip"));
    }
    Ok((bound, payload))
}

#[cfg(test)]
mod tests;
