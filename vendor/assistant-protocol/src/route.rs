//! A Stored Input's reviewed routes and Team's call-time match (`validators/route.py`, Assistant Spec v1).
//!
//! Every Stored Input declares `routes`: 1 to 32 entries `{"method", "path", "query"?}`, unique by method and path,
//! naming the only provider endpoints on its host that ever receive its value (ADR-0106 amendment). A path is at most
//! 512 characters of `/`-prefixed segments, each a literal of 1 to 64 unreserved characters other than `.` and `..`,
//! or `*` for exactly one concrete segment of 1 to 256 such characters. The optional `query` lists provider selectors:
//! a call to that route carries each named parameter exactly once with one listed raw value.
//!
//! A segment whose ASCII-lowercased text without `-`, `_`, `.`, and `~` contains a [`CREDENTIAL_STEMS`] entry names a
//! credential endpoint: it is refused as a declared literal and, at call time, as any concrete segment. Team matches a
//! call's request target as sent, path before query, and refuses rather than normalizes anything else.

use strict_json::Value;

use crate::python::{array, char_len, has_exactly, member, object, string};

/// The methods a route may name.
pub const METHODS: [&str; 6] = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"];
/// The most routes one Stored Input declares.
pub const MAX_ROUTES: usize = 32;
/// The longest route path, in characters.
pub const MAX_PATH: usize = 512;
/// The most selectors one route declares.
pub const MAX_SELECTORS: usize = 8;
/// The most values one selector lists.
pub const MAX_SELECTOR_VALUES: usize = 16;
/// The longest selector value, in characters.
pub const MAX_SELECTOR_VALUE: usize = 256;
/// The segment that matches exactly one concrete segment.
pub const WILDCARD: &str = "*";
/// The stems that make a segment name a credential endpoint.
pub const CREDENTIAL_STEMS: [&str; 7] = [
    "apikey",
    "authoriz",
    "credential",
    "oauth",
    "password",
    "secret",
    "token",
];

const fn unreserved(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'~' | b'-')
}

fn unreserved_text(text: &str, maximum: usize) -> bool {
    (1..=maximum).contains(&text.len()) && text.bytes().all(unreserved)
}

/// Whether one path segment names an endpoint that may issue, list, or exchange credentials.
#[must_use]
pub fn credential_segment(segment: &str) -> bool {
    let folded: String = segment
        .chars()
        .filter(|character| !matches!(character, '-' | '_' | '.' | '~'))
        .flat_map(char::to_lowercase)
        .collect();
    CREDENTIAL_STEMS.iter().any(|stem| folded.contains(stem))
}

/// The stable reason one Stored Input's declared routes are refused, or `None`.
#[must_use]
pub fn routes_error(routes: &Value) -> Option<&'static str> {
    let Some(routes) = array(routes).filter(|routes| (1..=MAX_ROUTES).contains(&routes.len()))
    else {
        return Some("routes_invalid");
    };
    let mut declared: Vec<(&str, &str)> = Vec::new();
    for route in routes {
        if let Some(error) = route_error(route) {
            return Some(error);
        }
        let key = method_and_path(route);
        if declared.contains(&key) {
            return Some("route_duplicate");
        }
        declared.push(key);
    }
    None
}

/// The method and path of an admitted route.
fn method_and_path(route: &Value) -> (&str, &str) {
    let text = |key| route.get(key).and_then(string).unwrap_or_default();
    (text("method"), text("path"))
}

/// The stable reason Team refuses to send a credential with these routes on one call, or `None`.
///
/// `target` is the request target as sent: the path, then an optional `?` and raw query.
#[must_use]
pub fn call_error(routes: &Value, method: &str, target: &str) -> Option<&'static str> {
    if let Some(error) = routes_error(routes) {
        return Some(error);
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let Some(rest) = path.strip_prefix('/') else {
        return Some("route_path");
    };
    let segments: Vec<&str> = rest.split('/').collect();
    if !segments.iter().all(|segment| concrete(segment)) {
        return Some("route_path");
    }
    let matched = array(routes).unwrap_or_default().iter().any(|route| {
        let (declared_method, pattern) = method_and_path(route);
        declared_method == method
            && matches(pattern, &segments)
            && selected(route.get("query"), query)
    });
    (!matched).then_some("route")
}

fn route_error(route: &Value) -> Option<&'static str> {
    let Some(members) = object(route) else {
        return Some("route_invalid");
    };
    let shaped = member(members, "method").is_some()
        && member(members, "path").is_some()
        && members
            .iter()
            .all(|(key, _)| matches!(key.as_str(), "method" | "path" | "query"));
    let method = member(members, "method").and_then(string);
    if !shaped || !method.is_some_and(|method| METHODS.contains(&method)) {
        return Some("route_invalid");
    }
    let path = member(members, "path").and_then(string);
    let Some(rest) = path
        .filter(|path| char_len(path) <= MAX_PATH)
        .and_then(|path| path.strip_prefix('/'))
    else {
        return Some("route_path_invalid");
    };
    if !rest
        .split('/')
        .all(|segment| segment == WILDCARD || literal(segment))
    {
        return Some("route_path_invalid");
    }
    if rest
        .split('/')
        .any(|segment| segment != WILDCARD && credential_segment(segment))
    {
        return Some("route_credential");
    }
    match member(members, "query") {
        Some(selectors) if !selectors_admitted(selectors) => Some("route_query_invalid"),
        _ => None,
    }
}

fn literal(segment: &str) -> bool {
    unreserved_text(segment, 64) && segment != "." && segment != ".."
}

fn concrete(segment: &str) -> bool {
    unreserved_text(segment, 256)
        && segment != "."
        && segment != ".."
        && !credential_segment(segment)
}

fn selector_name(text: &str) -> bool {
    unreserved_text(text, 64)
}

fn selectors_admitted(selectors: &Value) -> bool {
    let Some(selectors) =
        array(selectors).filter(|items| (1..=MAX_SELECTORS).contains(&items.len()))
    else {
        return false;
    };
    let mut names: Vec<String> = Vec::new();
    for selector in selectors {
        let Some(members) =
            object(selector).filter(|members| has_exactly(members, &["name", "values"]))
        else {
            return false;
        };
        let Some(name) = member(members, "name")
            .and_then(string)
            .filter(|name| selector_name(name))
        else {
            return false;
        };
        let folded = name.to_ascii_lowercase();
        if names.contains(&folded) || !member(members, "values").is_some_and(values_admitted) {
            return false;
        }
        names.push(folded);
    }
    true
}

/// `(?:[A-Za-z0-9._~-]|%[0-9A-F]{2})+`.
fn selector_value(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if unreserved(bytes[index]) {
            index += 1;
        } else if bytes[index] == b'%'
            && bytes.get(index + 1..index + 3).is_some_and(|hex| {
                hex.iter()
                    .all(|digit| matches!(digit, b'0'..=b'9' | b'A'..=b'F'))
            })
        {
            index += 3;
        } else {
            return false;
        }
    }
    !bytes.is_empty()
}

fn values_admitted(values: &Value) -> bool {
    let Some(values) =
        array(values).filter(|items| (1..=MAX_SELECTOR_VALUES).contains(&items.len()))
    else {
        return false;
    };
    let texts: Vec<&str> = values.iter().filter_map(string).collect();
    texts.len() == values.len()
        && texts
            .iter()
            .all(|value| char_len(value) <= MAX_SELECTOR_VALUE && selector_value(value))
        && texts
            .iter()
            .enumerate()
            .all(|(index, value)| !texts[..index].contains(value))
}

fn matches(pattern: &str, segments: &[&str]) -> bool {
    let expected: Vec<&str> = pattern.get(1..).unwrap_or_default().split('/').collect();
    expected.len() == segments.len()
        && expected
            .iter()
            .zip(segments)
            .all(|(part, segment)| *part == WILDCARD || part == segment)
}

/// Each selector appears exactly once, compared without case, under its exact name with one listed raw value; the
/// query has no `;` and only unreserved parameter names.
fn selected(selectors: Option<&Value>, query: &str) -> bool {
    let selectors = selectors.and_then(array).unwrap_or_default();
    if selectors.is_empty() {
        return true;
    }
    let pairs: Vec<(&str, &str)> = if query.is_empty() {
        Vec::new()
    } else {
        query
            .split('&')
            .map(|part| part.split_once('=').unwrap_or((part, "")))
            .collect()
    };
    if query.contains(';') || pairs.iter().any(|(name, _)| !selector_name(name)) {
        return false;
    }
    selectors
        .iter()
        .all(|selector| selector_matched(selector, &pairs))
}

fn selector_matched(selector: &Value, pairs: &[(&str, &str)]) -> bool {
    let name = selector.get("name").and_then(string).unwrap_or_default();
    let values = selector.get("values").and_then(array).unwrap_or_default();
    let found: Vec<&(&str, &str)> = pairs
        .iter()
        .filter(|(candidate, _)| candidate.eq_ignore_ascii_case(name))
        .collect();
    matches!(found.as_slice(), [(candidate, value)]
        if *candidate == name && values.iter().any(|listed| string(listed) == Some(*value)))
}
