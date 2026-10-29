//! A Team's Sign-in over the Team HTTP protocol (`sign_in.py`, ADR-0117).
//!
//! Each Team has at most one Sign-in, shown by its state, the sites found in its saved cookies, and its times, never its
//! capsule. Every body and answer has one exact form.

use std::sync::LazyLock;

use strict_json::Value;

use crate::browser::web_authority;
use crate::payload::canonical_locale;
use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{exact_object, field, is_bool, object_of, py_eq, small_int, string};
use crate::websocket::{self, authority, port};

/// A Sign-in or session id.
pub const ID_PATTERN: &str = r"[0-9a-f]{32}";
/// A Team incarnation: the Team network's Docker id.
pub const INCARNATION_PATTERN: &str = r"[0-9a-f]{64}";
/// A site: one lowercase DNS hostname of at least two labels.
pub const SITE_PATTERN: &str =
    r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}";
/// A second-precision UTC time.
pub const TIME_PATTERN: &str = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z";
/// The longest sign-in URL.
pub const MAX_URL_CHARS: usize = 2_048;
/// The longest Origin a request names.
pub const MAX_ORIGIN_CHARS: usize = 255;
/// The most sites a Sign-in shows.
pub const MAX_SITES: usize = 64;
/// The largest generation.
pub const MAX_GENERATION: i64 = (1 << 31) - 1;
/// A Sign-in's states, sorted.
pub const STATES: [&str; 2] = ["needs-person", "ready"];
/// The second factors a deletion confirms, sorted.
pub const FACTORS: [&str; 2] = ["passkey", "totp"];
/// A deletion's outcomes, sorted.
pub const DELETIONS: [&str; 2] = ["deleted", "pending"];
/// No sign-in session is running.
pub const NOT_RUNNING: &str = "sign-in-not-running";
/// The sign-in URL is invalid.
pub const URL_INVALID: &str = "sign-in-url-invalid";
/// The session opened other windows.
pub const WINDOWS: &str = "sign-in-windows";
/// The session reached an identity provider.
pub const IDENTITY_PROVIDER: &str = "sign-in-identity-provider";
/// The session has nothing to save.
pub const UNSAVED: &str = "sign-in-unsaved";
/// The Sign-in is busy.
pub const BUSY: &str = "sign-in-busy";
/// The Sign-in changed.
pub const CHANGED: &str = "sign-in-changed";
/// The Team has no Sign-in.
pub const ABSENT: &str = "sign-in-absent";

const SIGN_IN_KEYS: [&str; 10] = [
    "sign_in_id",
    "state",
    "generation",
    "team_incarnation",
    "sites",
    "sites_truncated",
    "created_at",
    "saved_at",
    "last_used_at",
    "expires_at",
];

/// [`ID_PATTERN`] compiled.
pub static ID_RE: LazyLock<Pattern> = LazyLock::new(|| declared(ID_PATTERN));
/// [`INCARNATION_PATTERN`] compiled.
pub static INCARNATION_RE: LazyLock<Pattern> = LazyLock::new(|| declared(INCARNATION_PATTERN));
/// [`SITE_PATTERN`] compiled.
pub static SITE_RE: LazyLock<Pattern> = LazyLock::new(|| declared(SITE_PATTERN));
/// [`TIME_PATTERN`] compiled.
pub static TIME_RE: LazyLock<Pattern> = LazyLock::new(|| declared(TIME_PATTERN));

/// `canonical_site`.
#[must_use]
pub fn canonical_site(value: &Value) -> Option<&str> {
    string(value).filter(|site| SITE_RE.is_full(site))
}

/// `start_site`: the site an https sign-in URL names, or `None`.
///
/// Its host is ASCII; it has no credentials, no port but 443, and nothing unprintable or spaced.
#[must_use]
pub fn start_site(url: &Value) -> Option<String> {
    let url = string(url)?;
    if text::len(url) > MAX_URL_CHARS
        || url
            .chars()
            .any(|character| text::isspace(character) || !text::isprintable_char(character))
    {
        return None;
    }
    let (scheme, netloc) = web_authority(url)?;
    let parts = authority(netloc);
    let credentials = parts.userinfo.is_some_and(|(name, password)| {
        !name.is_empty() || password.is_some_and(|text| !text.is_empty())
    });
    let admitted =
        scheme == "https" && !credentials && matches!(port(parts.port), Ok(None | Some(443)));
    let site = Value::String(parts.host.to_ascii_lowercase());
    admitted
        .then(|| canonical_site(&site).map(str::to_owned))
        .flatten()
}

fn id(value: &Value) -> bool {
    string(value).is_some_and(|id| ID_RE.is_full(id))
}

fn generation(value: &Value) -> bool {
    small_int(value).is_some_and(|generation| (1..=MAX_GENERATION).contains(&generation))
}

fn incarnation(value: &Value) -> bool {
    string(value).is_some_and(|incarnation| INCARNATION_RE.is_full(incarnation))
}

fn member(value: &Value, choices: &[&str]) -> bool {
    string(value).is_some_and(|text| choices.contains(&text))
}

/// `canonical_origin`: the exact, bounded Origin of the Admin page a request came from, as Admin verified it.
#[must_use]
pub fn canonical_origin(value: &Value) -> Option<&str> {
    string(value).filter(|origin| {
        text::len(origin) <= MAX_ORIGIN_CHARS
            && websocket::canonical_origin(Some(origin)).as_deref() == Some(*origin)
    })
}

fn time(value: &Value) -> Option<&str> {
    string(value).filter(|moment| TIME_RE.is_full(moment))
}

/// `canonical_start`: `{url, locale, origin}`.
#[must_use]
pub fn canonical_start(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["url", "locale", "origin"])?;
    (start_site(field(members, "url")).is_some()
        && canonical_locale(field(members, "locale")).is_some()
        && canonical_origin(field(members, "origin")).is_some())
    .then_some(value)
}

/// `canonical_session`: `{sign_in_session_id, origin}`.
#[must_use]
pub fn canonical_session(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["sign_in_session_id", "origin"])?;
    (id(field(members, "sign_in_session_id"))
        && canonical_origin(field(members, "origin")).is_some())
    .then_some(value)
}

/// `canonical_delete`: the exact Sign-in, generation, and Team incarnation confirmed, the factor, and the Origin.
#[must_use]
pub fn canonical_delete(value: &Value) -> Option<&Value> {
    let members = exact_object(
        value,
        &[
            "sign_in_id",
            "generation",
            "team_incarnation",
            "factor",
            "origin",
        ],
    )?;
    (id(field(members, "sign_in_id"))
        && generation(field(members, "generation"))
        && incarnation(field(members, "team_incarnation"))
        && member(field(members, "factor"), &FACTORS)
        && canonical_origin(field(members, "origin")).is_some())
    .then_some(value)
}

/// `canonical_started`: a started session's id, its view, and the site it starts at.
#[must_use]
pub fn canonical_started(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["sign_in_session_id", "view_id", "site"])?;
    (id(field(members, "sign_in_session_id"))
        && id(field(members, "view_id"))
        && canonical_site(field(members, "site")).is_some())
    .then_some(value)
}

fn sites(value: &Value) -> bool {
    let Value::Array(items) = value else {
        return false;
    };
    let names: Vec<&str> = items.iter().filter_map(canonical_site).collect();
    names.len() == items.len()
        && items.len() <= MAX_SITES
        && names.windows(2).all(|pair| pair[0] < pair[1])
}

/// `canonical_sign_in`: the Team's Sign-in as Admin shows it, never its capsule.
#[must_use]
pub fn canonical_sign_in(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &SIGN_IN_KEYS)?;
    let created = time(field(members, "created_at"))?;
    let saved = time(field(members, "saved_at"))?;
    let used = time(field(members, "last_used_at"))?;
    let expires = time(field(members, "expires_at"))?;
    (id(field(members, "sign_in_id"))
        && member(field(members, "state"), &STATES)
        && generation(field(members, "generation"))
        && incarnation(field(members, "team_incarnation"))
        && sites(field(members, "sites"))
        && is_bool(field(members, "sites_truncated"))
        && created <= saved
        && saved < expires
        && used >= created)
        .then_some(value)
}

/// `canonical_current`: `{sign_in}`, the Team's Sign-in or null.
#[must_use]
pub fn canonical_current(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["sign_in"])?;
    let sign_in = field(members, "sign_in");
    (*sign_in == Value::Null || canonical_sign_in(sign_in).is_some()).then_some(value)
}

/// `canonical_saved`: `{sign_in, recorded}`.
#[must_use]
pub fn canonical_saved(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["sign_in", "recorded"])?;
    (canonical_sign_in(field(members, "sign_in")).is_some() && is_bool(field(members, "recorded")))
        .then_some(value)
}

/// `canonical_deleted`: `{deletion}`, `deleted` or `pending`.
#[must_use]
pub fn canonical_deleted(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["deletion"])?;
    member(field(members, "deletion"), &DELETIONS).then_some(value)
}

/// `canonical_cancelled`: a value equal to `{"cancelled": true}` as Python compares it.
#[must_use]
pub fn canonical_cancelled(value: &Value) -> Option<&Value> {
    py_eq(value, &object_of(vec![("cancelled", Value::Bool(true))])).then_some(value)
}

#[cfg(test)]
mod tests;
