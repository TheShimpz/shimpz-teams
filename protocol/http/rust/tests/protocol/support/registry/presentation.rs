//! `content`, `preview`, `browser_view`, `challenge`, and `supervisor`.

use strict_json::{Number, Value};
use team_protocol::{browser_view, challenge, content, preview, supervisor};

use super::{Entry, Outcome, arguments, borrowed, string, text, value};

fn page(value: &Value) -> Outcome {
    preview::canonical_page(value).map_or(Outcome::Refuse, |page| {
        Outcome::Value(Value::Number(Number::Int(page)))
    })
}

fn accept(value: &Value) -> Outcome {
    browser_view::canonical_key(value).map_or(Outcome::Refuse, |key| {
        Outcome::Value(Value::String(browser_view::accept(key)))
    })
}

fn rendered(value: &Value) -> Outcome {
    arguments(value, ["value", "request"]).map_or(Outcome::Refuse, |[rendered, request]| {
        borrowed(challenge::canonical_rendered(rendered, request))
    })
}

fn encoded(result: Result<Vec<u8>, supervisor::SupervisorAssertionError>) -> Outcome {
    result.map_or(Outcome::Error("supervisor"), |bytes| {
        Outcome::Value(Value::String(String::from_utf8(bytes).expect("UTF-8 JSON")))
    })
}

/// The claims and audience of an argument object whose audience is a string.
fn claims(value: &Value) -> Option<(&Value, &str)> {
    let [claims, audience] = arguments(value, ["value", "audience"])?;
    string(audience).map(|audience| (claims, audience))
}

fn canonical_claims(value: &Value) -> Outcome {
    claims(value).map_or(Outcome::Refuse, |(claims, audience)| {
        supervisor::canonical_claims(claims, audience)
            .map_or(Outcome::Error("supervisor"), Outcome::Value)
    })
}

fn claims_json(value: &Value) -> Outcome {
    claims(value).map_or(Outcome::Refuse, |(claims, audience)| {
        encoded(supervisor::claims_json(claims, audience))
    })
}

/// The entries of the presentation and Supervisor modules.
pub const ENTRIES: &[Entry] = &[
    ("content.canonical_framing", |v| {
        borrowed(content::canonical_framing(v))
    }),
    ("preview.canonical_framing", |v| {
        borrowed(preview::canonical_framing(v))
    }),
    ("preview.canonical_page", page),
    ("preview.canonical_page_framing", |v| {
        borrowed(preview::canonical_page_framing(v))
    }),
    ("browser_view.canonical_view", |v| {
        borrowed(browser_view::canonical_view(v))
    }),
    ("browser_view.canonical_key", |v| {
        text(browser_view::canonical_key(v))
    }),
    ("browser_view.accept", accept),
    ("challenge.canonical_rendered", rendered),
    ("challenge.canonical_file_disclosure", |v| {
        value(challenge::canonical_file_disclosure(v))
    }),
    ("challenge.canonical_input_projection", |v| {
        borrowed(challenge::canonical_input_projection(v))
    }),
    ("supervisor.canonical_claims", canonical_claims),
    ("supervisor.canonical_json", |v| {
        encoded(supervisor::canonical_json(v))
    }),
    ("supervisor.claims_json", claims_json),
    ("supervisor.canonical_key_rotation", |v| {
        value(supervisor::canonical_key_rotation(v))
    }),
];
