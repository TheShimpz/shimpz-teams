//! `identifiers`, `purpose`, and `payload`, and the Shimpz canonical JSON v1 writer (`canonical_json.encode`).

use strict_json::{Number, Value};
use team_protocol::python::{datetime, json};
use team_protocol::{identifiers, payload, purpose};

use super::{Entry, Outcome, arguments, borrowed, flag, small, string, text, value};

fn strings(items: &[String]) -> Value {
    Value::Array(items.iter().cloned().map(Value::String).collect())
}

fn integer(value: &Value) -> Outcome {
    let Some([number, minimum]) = arguments(value, ["value", "minimum"]) else {
        return Outcome::Refuse;
    };
    match small(minimum) {
        Some(minimum @ 0..=1) => {
            let minimum = u8::try_from(minimum).expect("0 or 1");
            borrowed(payload::integer(number, minimum).map(|_| number))
        }
        _ => Outcome::Refuse,
    }
}

fn file_metadata(value: &Value) -> Outcome {
    match arguments(value, ["value", "include_usage"]) {
        Some([metadata, Value::Bool(usage)]) => {
            super::value(payload::project_file_metadata(metadata, *usage))
        }
        _ => Outcome::Refuse,
    }
}

fn storage_response(value: &Value) -> Outcome {
    let Some([response, kind, team]) = arguments(value, ["value", "kind", "expected_team_id"])
    else {
        return Outcome::Refuse;
    };
    match (string(kind), string(team)) {
        (Some(kind), Some(team)) => {
            super::value(payload::project_storage_response(response, kind, team))
        }
        _ => Outcome::Refuse,
    }
}

fn fresh(value: &Value) -> Outcome {
    match arguments(value, ["issued_at", "now"]).map(|[issued, now]| (small(issued), small(now))) {
        Some((Some(issued), Some(now)))
            if issued.unsigned_abs() < 1 << 50 && now.unsigned_abs() < 1 << 50 =>
        {
            flag(payload::request_identity_fresh(issued, now))
        }
        _ => Outcome::Refuse,
    }
}

fn rendered(value: &Value) -> Outcome {
    match arguments(value, ["text", "reference", "maximum", "nullable"]) {
        Some([text, reference, maximum, Value::Bool(nullable)]) => match small(maximum) {
            Some(maximum @ 0..=1_000_000) => {
                let maximum = usize::try_from(maximum).expect("a small bound");
                flag(payload::rendered_text(text, reference, maximum, *nullable))
            }
            _ => Outcome::Refuse,
        },
        _ => Outcome::Refuse,
    }
}

fn compose(value: &Value) -> Outcome {
    let Some(parts) = arguments(value, ["original", "question", "answer", "locale"]) else {
        return Outcome::Refuse;
    };
    match parts.map(string) {
        [Some(original), Some(question), Some(answer), Some(locale)] => {
            text(payload::compose_clarified(original, question, answer, locale).as_deref())
        }
        _ => Outcome::Refuse,
    }
}

fn segments(value: &Value) -> Outcome {
    string(value).map_or(Outcome::Refuse, |message| {
        Outcome::Value(strings(&payload::authored_segments(message)))
    })
}

fn render(value: &Value) -> Outcome {
    payload::canonical_clarification(value).map_or(Outcome::Refuse, |clarification| {
        Outcome::Value(Value::String(payload::render_clarification(&clarification)))
    })
}

fn apply(value: &Value) -> Outcome {
    let Some([memory, changes]) = arguments(value, ["memory", "changes"]) else {
        return Outcome::Refuse;
    };
    match (
        payload::canonical_memory(memory),
        payload::canonical_memory_changes(changes),
    ) {
        (Some(memory), Some(changes)) => {
            Outcome::Value(payload::apply_memory_changes(&memory, &changes))
        }
        _ => Outcome::Refuse,
    }
}

fn media_type(value: &Value) -> Outcome {
    text(payload::canonical_media_type(value).as_deref())
}

fn fromisoformat(value: &Value) -> Outcome {
    let Some(parsed) = string(value).and_then(|text| datetime::fromisoformat(text).ok()) else {
        return Outcome::Refuse;
    };
    let fields = [
        parsed.year,
        parsed.month,
        parsed.day,
        parsed.hour,
        parsed.minute,
        parsed.second,
        parsed.microsecond,
    ];
    let mut items: Vec<Value> = fields
        .into_iter()
        .map(|field| Value::Number(Number::Int(i64::from(field))))
        .collect();
    items.push(
        parsed
            .offset
            .map_or(Value::Null, |offset| Value::Number(Number::Int(offset))),
    );
    Outcome::Value(Value::Array(items))
}

fn encode(value: &Value) -> Outcome {
    Outcome::Value(Value::String(
        String::from_utf8(json::canonical(value)).expect("canonical JSON is UTF-8"),
    ))
}

/// The entries of these modules.
pub const ENTRIES: &[Entry] = &[
    ("datetime.fromisoformat", fromisoformat),
    ("canonical_json.encode", encode),
    ("identifiers.canonical_team_id", |v| {
        text(identifiers::canonical_team_id(v))
    }),
    ("identifiers.canonical_assistant_id", |v| {
        text(identifiers::canonical_assistant_id(v))
    }),
    ("identifiers.canonical_identifier", |v| {
        text(identifiers::canonical_identifier(v))
    }),
    ("identifiers.canonical_action_id", |v| {
        text(identifiers::canonical_action_id(v))
    }),
    ("purpose.canonical_purpose", |v| {
        text(purpose::canonical_purpose(v))
    }),
    ("payload.canonical_conversation", |v| {
        value(payload::canonical_conversation(v))
    }),
    ("payload.canonical_locale", |v| {
        text(payload::canonical_locale(v))
    }),
    ("payload.request_identity_fresh", fresh),
    ("payload.canonical_request_identity", |v| {
        value(payload::canonical_request_identity(v))
    }),
    ("payload.canonical_help_url", |v| {
        text(payload::canonical_help_url(v))
    }),
    ("payload.canonical_stored_input_help", |v| {
        text(payload::canonical_stored_input_help(v))
    }),
    ("payload.canonical_creator_links", |v| {
        value(payload::canonical_creator_links(v))
    }),
    ("payload.canonical_pack_digest", |v| {
        text(payload::canonical_pack_digest(v))
    }),
    ("payload.rendered_text", rendered),
    ("payload.canonical_snapshot_summary", |v| {
        borrowed(payload::canonical_snapshot_summary(v))
    }),
    ("payload.canonical_local_assistant", |v| {
        borrowed(payload::canonical_local_assistant(v))
    }),
    ("payload.canonical_created_at", |v| {
        text(payload::canonical_created_at(v))
    }),
    ("payload.canonical_assistant_details", |v| {
        borrowed(payload::canonical_assistant_details(v))
    }),
    ("payload.canonical_turn_usage", |v| {
        value(payload::canonical_turn_usage(v))
    }),
    ("payload.canonical_run_usage", |v| {
        value(payload::canonical_run_usage(v))
    }),
    ("payload.canonical_clarification", |v| {
        value(payload::canonical_clarification(v))
    }),
    ("payload.compose_clarified", compose),
    ("payload.authored_segments", segments),
    ("payload.render_clarification", render),
    ("payload.canonical_memory", |v| {
        value(payload::canonical_memory(v))
    }),
    ("payload.canonical_memory_changes", |v| {
        value(payload::canonical_memory_changes(v))
    }),
    ("payload.apply_memory_changes", apply),
    ("payload.canonical_source_digest", |v| {
        text(payload::canonical_source_digest(v))
    }),
    ("payload.canonical_team_name", |v| {
        text(payload::canonical_team_name(v))
    }),
    ("payload.canonical_chat_limit", |v| {
        value(payload::canonical_chat_limit(v))
    }),
    ("payload.canonical_local_team_name", |v| {
        text(payload::canonical_local_team_name(v))
    }),
    ("payload.canonical_file_id", |v| {
        text(payload::canonical_file_id(v))
    }),
    ("payload.canonical_filename", |v| {
        text(payload::canonical_filename(v))
    }),
    ("payload.canonical_media_type", media_type),
    ("payload.integer", integer),
    ("payload.project_storage_usage", |v| {
        value(payload::project_storage_usage(v))
    }),
    ("payload.project_file_metadata", file_metadata),
    ("payload.project_storage_response", storage_response),
];
