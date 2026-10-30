//! Bounded Team-owned challenges for admitted Action human requests (`action/challenges.py`, ADR-0090, ADR-0091).
//!
//! A challenge binds one admitted request to its copy rendered in one interface language from the binding's exact
//! catalog and pack, and projects only public reviewed context: never the Action input (an authorization card shows
//! only its bounded, escaped projection), the transcript, or a private value. One live challenge per Team, one use,
//! five minutes; expired continuations are drained for their owner to clean.

use std::sync::Arc;
use std::time::Duration;

use assistant_protocol::catalog::message_id;
use assistant_protocol::catalog::render;
use strict_json::{Number, Value};
use team_assistant::language::LanguagePack;
use team_core::entropy::Entropy;
use team_integrations::challenge::{
    ChallengeError, ChallengeStore, Limits, MAX_PENDING_CHALLENGES, Pending,
};
use team_protocol::challenge::{
    INPUT_PROJECTION_MAX_FIELDS, INPUT_PROJECTION_NAME_CHARS, INPUT_PROJECTION_VALUE_CHARS,
    canonical_file_disclosure, canonical_input_projection, canonical_rendered,
};
use team_protocol::payload::{
    canonical_help_url, canonical_locale, canonical_pack_digest, canonical_purpose,
    canonical_stored_input_help,
};
use team_protocol::python::json::{self, Options};
use team_protocol::python::text::isprintable_char;
use tokio::time::Instant;

use crate::human::{self, COPY_FIELDS, HumanRequest, OPTION_COPY_FIELDS};

/// A human challenge's lifetime.
pub const DEFAULT_TTL: Duration = Duration::from_secs(300);

/// A challenge could not be rendered, created, or projected.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HumanChallengeError(pub &'static str);

/// A request's copy rendered for one interface language from one exact binding.
#[derive(Clone, Debug, PartialEq)]
pub struct RequestCopy {
    /// The interface language.
    pub locale: String,
    /// The binding's catalog digest.
    pub catalog_digest: String,
    /// The binding's pack digest.
    pub pack_digest: String,
    /// The rendered copy fields.
    pub rendered: Value,
    /// The rendered help text of the Stored Input a password request names.
    pub help: Option<String>,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// Renders every copy field of an admitted request in `locale` from its binding's pack; English is the catalog.
///
/// # Errors
/// [`HumanChallengeError`] for an unknown locale, a pack whose messages differ from the request's binding, or copy
/// that cannot be rendered as bounded public text.
pub fn render_copy(
    request: &HumanRequest,
    pack: &LanguagePack,
    locale: &str,
    help_text: Option<&str>,
) -> Result<RequestCopy, HumanChallengeError> {
    if canonical_locale(&text(locale)).is_none() {
        return Err(HumanChallengeError(
            "Action human request locale is invalid",
        ));
    }
    let payload = request.payload();
    let Value::Array(messages) = request.messages() else {
        return Err(HumanChallengeError(
            "Action human request copy does not match its binding",
        ));
    };
    let packed = assistant_protocol::catalog::catalog_by_id(&pack.messages);
    let mut referenced = human::referenced_messages(&payload);
    referenced.sort();
    referenced.dedup();
    let ids: Vec<String> = messages
        .iter()
        .filter_map(|message| match message.get("id") {
            Some(Value::String(id)) => Some(id.clone()),
            _ => None,
        })
        .collect();
    let same = ids == referenced
        && messages.iter().zip(&ids).all(|(message, id)| {
            packed.get(id.as_str()).is_some_and(|entry| {
                crate::journal::canonical(&Value::Object(entry.to_vec()))
                    == crate::journal::canonical(message)
            })
        });
    if !same {
        return Err(HumanChallengeError(
            "Action human request copy does not match its binding",
        ));
    }
    let rendered = rendered_copy(&payload, pack, locale)?;
    let help = help_text
        .map(|help| render_help(help, pack, locale))
        .transpose()?;
    Ok(RequestCopy {
        locale: locale.to_owned(),
        catalog_digest: pack.catalog_digest.clone(),
        pack_digest: pack.pack_digest.clone(),
        rendered,
        help,
    })
}

fn rendered_copy(
    payload: &Value,
    pack: &LanguagePack,
    locale: &str,
) -> Result<Value, HumanChallengeError> {
    let render_reference = |reference: Option<&Value>| match reference {
        Some(reference @ Value::Object(_)) => match reference.get("message") {
            Some(Value::String(id)) => pack
                .template(id, locale)
                .map(|template| text(&render(reference, &template))),
            _ => None,
        },
        _ => Some(Value::Null),
    };
    let mut rendered = Vec::new();
    for field in COPY_FIELDS {
        if payload.get(field).is_some() {
            let value = render_reference(payload.get(field)).ok_or(HumanChallengeError(
                "Action human request copy cannot be rendered",
            ))?;
            rendered.push((field.to_owned(), value));
        }
    }
    if let Some(Value::Array(options)) = payload.get("options") {
        let mut copies = Vec::new();
        for option in options {
            let mut copy = Vec::new();
            for field in OPTION_COPY_FIELDS {
                let value = render_reference(option.get(field)).ok_or(HumanChallengeError(
                    "Action human request copy cannot be rendered",
                ))?;
                copy.push((field.to_owned(), value));
            }
            copies.push(Value::Object(copy));
        }
        rendered.push(("options".to_owned(), Value::Array(copies)));
    }
    let rendered = Value::Object(rendered);
    if canonical_rendered(&rendered, payload).is_none() {
        return Err(HumanChallengeError(
            "Action human request copy cannot be rendered",
        ));
    }
    Ok(rendered)
}

fn render_help(
    help: &str,
    pack: &LanguagePack,
    locale: &str,
) -> Result<String, HumanChallengeError> {
    let id = message_id(help);
    let catalog = assistant_protocol::catalog::catalog_by_id(&pack.messages);
    let parameterless = catalog.get(id.as_str()).is_some_and(|message| {
        Value::Object(message.to_vec()).get("params") == Some(&Value::Array(Vec::new()))
    });
    if !parameterless {
        return Err(HumanChallengeError(
            "Action Stored Input help text does not match its binding",
        ));
    }
    let template = pack.template(&id, locale).ok_or(HumanChallengeError(
        "Action Stored Input help text cannot be rendered",
    ))?;
    canonical_stored_input_help(&text(&template))
        .map(str::to_owned)
        .ok_or(HumanChallengeError(
            "Action Stored Input help text cannot be rendered",
        ))
}

/// Public context for one exact request, without Action input or private values.
#[derive(Clone, Debug, PartialEq)]
pub struct HumanRequirement {
    /// The Assistant.
    pub assistant_id: String,
    /// Its public name.
    pub assistant_name: String,
    /// The Action.
    pub action_id: String,
    /// Its reviewed summary.
    pub action_summary: String,
    /// The Brain interrupt.
    pub interrupt_id: String,
    /// The admitted request.
    pub request: HumanRequest,
    /// The Assistant's version.
    pub assistant_version: String,
    /// The rendered copy.
    pub copy: RequestCopy,
    /// The Stored Input's reviewed help link.
    pub help_url: Option<String>,
    /// The Stored Input's English help text.
    pub help_text: Option<String>,
    /// The Brain's task-bound sentence for why the Action pauses (ADR-0090).
    pub purpose: Option<String>,
    /// The locale the purpose was written in.
    pub purpose_locale: Option<String>,
    /// The one selected file an authorization of a file-taking Action discloses.
    pub file: Option<Value>,
    /// The projection of the validated input an authorization card shows.
    pub input: Option<Value>,
}

impl HumanRequirement {
    /// Whether every field holds its contract.
    #[must_use]
    pub fn valid(&self) -> bool {
        let copy = &self.copy;
        let copy_valid = canonical_locale(&text(&copy.locale)).is_some()
            && canonical_pack_digest(&text(&copy.catalog_digest)).is_some()
            && canonical_pack_digest(&text(&copy.pack_digest)).is_some()
            && canonical_rendered(&copy.rendered, &self.request.payload()).is_some()
            && copy
                .help
                .as_ref()
                .is_none_or(|help| canonical_stored_input_help(&text(help)) == Some(help.as_str()));
        let password = self.request.kind == "input:password" && self.request.stored_input.is_some();
        let help_valid = if password {
            self.help_text
                .as_ref()
                .is_some_and(|help| canonical_stored_input_help(&text(help)) == Some(help.as_str()))
                && copy.help.is_some()
                && self
                    .help_url
                    .as_ref()
                    .is_some_and(|url| canonical_help_url(&text(url)) == Some(url.as_str()))
        } else {
            self.help_text.is_none() && copy.help.is_none() && self.help_url.is_none()
        };
        let purpose_valid = match (&self.purpose, &self.purpose_locale) {
            (None, None) => true,
            (Some(purpose), Some(locale)) => {
                canonical_purpose(&text(purpose)) == Some(purpose.as_str())
                    && canonical_locale(&text(locale)).is_some()
            }
            _ => false,
        };
        let authorizes = human::authorizes(&self.request.kind);
        let input_valid = self
            .input
            .as_ref()
            .is_none_or(|input| authorizes && canonical_input_projection(input).is_some());
        let file_valid = self.file.as_ref().is_none_or(|file| {
            authorizes && canonical_file_disclosure(file).as_ref() == Some(file)
        });
        copy_valid
            && (1..=40).contains(&self.assistant_version.chars().count())
            && help_valid
            && purpose_valid
            && input_valid
            && file_valid
    }
}

/// The human challenge store: metadata is the requirement, `P` the private continuation; expired ones are drained.
pub type HumanChallengeStore<P> = ChallengeStore<HumanRequirement, P>;

/// The store with its default capacity and lifetime.
///
/// # Errors
/// Never in practice: the defaults are in range.
pub fn store<P>(entropy: Arc<dyn Entropy>) -> Result<HumanChallengeStore<P>, ChallengeError> {
    ChallengeStore::new(
        Limits {
            capacity: MAX_PENDING_CHALLENGES,
            ttl: DEFAULT_TTL,
            retain_expired: true,
        },
        HumanRequirement::valid,
        entropy,
    )
}

/// The public modal descriptor of one challenge, without Action input or response material.
///
/// # Errors
/// [`HumanChallengeError`] for an invalid or expired challenge.
pub fn challenge_payload<P>(
    challenge: &Pending<HumanRequirement, P>,
) -> Result<Value, HumanChallengeError> {
    let requirement = &challenge.metadata;
    if !requirement.valid() {
        return Err(HumanChallengeError("Action human challenge is invalid"));
    }
    let remaining = challenge
        .expires_at
        .saturating_duration_since(Instant::now());
    let seconds = remaining.as_secs() + u64::from(remaining.subsec_nanos() > 0);
    if !(1..=DEFAULT_TTL.as_secs()).contains(&seconds) {
        return Err(HumanChallengeError("Action human challenge is expired"));
    }
    let object = |members: Vec<(&str, Value)>| {
        Value::Object(
            members
                .into_iter()
                .map(|(name, value)| (name.to_owned(), value))
                .collect(),
        )
    };
    let mut members = vec![
        ("team_id", text(&challenge.team_id)),
        ("status", text("human-required")),
        ("turn_id", text(&challenge.id)),
        ("challenge_id", text(&challenge.id)),
        (
            "expires_in",
            Value::Number(Number::Int(i64::try_from(seconds).unwrap_or(i64::MAX))),
        ),
        (
            "assistant",
            object(vec![
                ("id", text(&requirement.assistant_id)),
                ("name", text(&requirement.assistant_name)),
                ("version", text(&requirement.assistant_version)),
            ]),
        ),
        (
            "action",
            object(vec![
                ("id", text(&requirement.action_id)),
                ("summary", text(&requirement.action_summary)),
            ]),
        ),
        ("request", requirement.request.payload()),
        ("rendered", requirement.copy.rendered.clone()),
        ("locale", text(&requirement.copy.locale)),
        ("pack_digest", text(&requirement.copy.pack_digest)),
    ];
    members.extend(optional_members(requirement));
    Ok(object(members))
}

/// The members a challenge projects only when its requirement carries them.
fn optional_members(requirement: &HumanRequirement) -> Vec<(&'static str, Value)> {
    let mut members = Vec::new();
    // A purpose is written in its turn's language, so only a challenge in that same locale shows it (ADR-0091).
    if let (Some(purpose), Some(locale)) = (&requirement.purpose, &requirement.purpose_locale)
        && *locale == requirement.copy.locale
    {
        members.push(("purpose", text(purpose)));
    }
    if let (Some(help), Some(url)) = (&requirement.copy.help, &requirement.help_url) {
        members.push(("help", text(help)));
        members.push(("help_url", text(url)));
    }
    if let Some(file) = &requirement.file {
        members.push(("file", file.clone()));
    }
    if let Some(input) = &requirement.input {
        members.push(("input", input.clone()));
    }
    members
}

/// JSON text with every character that is not printable, such as a bidirectional control, made visible.
fn escaped(text: &str) -> String {
    use std::fmt::Write as _;
    let mut out = String::with_capacity(text.len());
    for character in text.chars() {
        if isprintable_char(character) {
            out.push(character);
        } else {
            let mut units = [0_u16; 2];
            for unit in character.encode_utf16(&mut units) {
                let _ = write!(out, "\\u{unit:04x}");
            }
        }
    }
    out
}

/// `text` cut to `maximum` characters, never inside an escape, and whether it was cut.
fn bounded(text: &str, maximum: usize) -> (String, bool) {
    let characters: Vec<char> = text.chars().collect();
    if characters.len() <= maximum {
        return (text.to_owned(), false);
    }
    let mut cut: String = characters[..maximum].iter().collect();
    let cut_chars: Vec<char> = cut.chars().collect();
    if let Some(escape) = (0..cut_chars.len().saturating_sub(1))
        .rev()
        .find(|index| cut_chars[*index] == '\\' && cut_chars[index + 1] == 'u')
    {
        let run = cut_chars[..escape]
            .iter()
            .rev()
            .take_while(|character| **character == '\\')
            .count();
        if cut_chars.len() - escape < 6 && run % 2 == 0 {
            cut = cut_chars[..escape].iter().collect();
        }
    }
    (cut, true)
}

const PROJECTION_JSON: Options = Options {
    ensure_ascii: false,
    sort_keys: true,
    compact: false,
};

/// The validated input as the bounded, escaped rows an authorization card shows: each top-level argument's escaped
/// name and escaped canonical JSON value, sorted; a row cut to its bound says so, and arguments past the last row are
/// counted.
///
/// # Errors
/// [`HumanChallengeError`] when the projection would not satisfy the wire contract.
pub fn input_projection(arguments: &Value) -> Result<Value, HumanChallengeError> {
    let Value::Object(members) = arguments else {
        return Err(HumanChallengeError("Action input cannot be projected"));
    };
    let mut rows: Vec<(String, String)> = members
        .iter()
        .map(|(name, value)| {
            let quoted = json::dumps(&Value::String(name.clone()), PROJECTION_JSON);
            (
                escaped(&quoted[1..quoted.len() - 1]),
                escaped(&json::dumps(value, PROJECTION_JSON)),
            )
        })
        .collect();
    rows.sort();
    let fields: Vec<Value> = rows
        .iter()
        .take(INPUT_PROJECTION_MAX_FIELDS)
        .map(|(name, value)| {
            let (name, name_cut) = bounded(name, INPUT_PROJECTION_NAME_CHARS);
            let (value, value_cut) = bounded(value, INPUT_PROJECTION_VALUE_CHARS);
            Value::Object(vec![
                ("name".to_owned(), Value::String(name)),
                ("value".to_owned(), Value::String(value)),
                ("truncated".to_owned(), Value::Bool(name_cut || value_cut)),
            ])
        })
        .collect();
    let omitted = rows.len() - fields.len();
    let projection = Value::Object(vec![
        ("fields".to_owned(), Value::Array(fields)),
        (
            "omitted".to_owned(),
            Value::Number(Number::Int(i64::try_from(omitted).unwrap_or(i64::MAX))),
        ),
    ]);
    canonical_input_projection(&projection)
        .cloned()
        .ok_or(HumanChallengeError("Action input cannot be projected"))
}

#[cfg(test)]
mod tests;
