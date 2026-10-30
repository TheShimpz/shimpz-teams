//! The credentials one Action declares, placed for injection, and the checks that keep them Team's.

use std::collections::BTreeSet;

use hmac::digest::KeyInit;
use hmac::{Hmac, Mac};
use sha2::Sha256;
use strict_json::Value;
use team_core::secret::Secret;
use team_integrations::providers::Provider;
use team_protocol::python::text::lower;

use super::frame::field_value;
use super::{Call, Refusal};
use crate::failure::encodings;

/// The endpoints a credential may be sent to.
#[derive(Clone, Debug)]
pub enum Routes {
    /// A Stored Input's reviewed routes and selectors, matched by the Assistant protocol on the raw method and target.
    Declared(Value),
    /// An Integration provider's reviewed routes, matched on the path as sent, before any query.
    Provider(&'static Provider),
}

impl Routes {
    /// Whether this credential may travel on `method` to `target`.
    #[must_use]
    pub fn admits(&self, method: &str, target: &str) -> bool {
        match self {
            Self::Declared(routes) => {
                assistant_protocol::route::call_error(routes, method, target).is_none()
            }
            Self::Provider(provider) => provider.allows(
                method,
                target.split_once('?').map_or(target, |(path, _)| path),
            ),
        }
    }
}

/// One credential placed in calls to its host: a header or a query parameter, never both.
#[derive(Clone, Debug)]
pub struct Credential {
    /// `stored-input:<id>` or `integration:<id>`, as the audit names it.
    pub id: String,
    /// The host it goes to.
    pub host: String,
    /// The lowercase header it is placed in.
    pub header: Option<String>,
    /// The query parameter it is placed in.
    pub query: Option<String>,
    /// The placed value.
    pub value: Secret,
    /// Every raw and placed form a response must never echo.
    pub protected: Vec<Secret>,
    /// Where it may be sent.
    pub routes: Routes,
}

/// One Stored Input's reviewed placement.
#[derive(Clone, Debug, PartialEq)]
pub struct Placement {
    /// The host it goes to.
    pub host: String,
    /// The header it is placed in.
    pub header: Option<String>,
    /// The query parameter it is placed in.
    pub query: Option<String>,
    /// A scheme prefixed to the placed value (`Bearer`).
    pub scheme: Option<String>,
    /// The Stored Input this one signs: the placed value is HMAC-SHA256(this value, that value) in hex.
    pub hmac: Option<String>,
    /// The reviewed routes.
    pub routes: Value,
}

/// The lowercase hexadecimal HMAC-SHA256 keyed by `key` over `message`, as Meta's `appsecret_proof`.
fn proof(key: &str, message: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(key.as_bytes()).unwrap_or_else(|_| unreachable!());
    mac.update(message.as_bytes());
    crate::journal::hex(&mac.finalize().into_bytes())
}

/// The credentials one Action declares, placed for injection, and the hosts whose declared credential Team does not
/// hold. `stored` lists the Action's Stored Inputs in declared order with their placements; `held` is the attempt's
/// held values; `integrations` lists the Action's Integrations with their provider and held access token.
#[must_use]
pub fn action_credentials(
    stored: &[(String, Placement)],
    held: &[(String, Secret)],
    integrations: &[(String, &'static Provider, Option<Secret>)],
) -> (Vec<Credential>, BTreeSet<String>) {
    let (mut credentials, mut missing) = stored_credentials(stored, held);
    for (id, provider, token) in integrations {
        let Some(token) = token.as_ref().filter(|token| !token.expose().is_empty()) else {
            missing.extend(provider.api_hosts.iter().map(|host| (*host).to_owned()));
            continue;
        };
        let bearer = Secret::new(format!("Bearer {}", token.expose()));
        credentials.extend(provider.api_hosts.iter().map(|host| Credential {
            id: format!("integration:{id}"),
            host: (*host).to_owned(),
            header: Some("authorization".to_owned()),
            query: None,
            value: bearer.clone(),
            protected: vec![token.clone(), bearer.clone()],
            routes: Routes::Provider(provider),
        }));
    }
    (credentials, missing)
}

/// The placed Stored Input credentials and the hosts whose value or proof key Team does not hold.
fn stored_credentials(
    stored: &[(String, Placement)],
    held: &[(String, Secret)],
) -> (Vec<Credential>, BTreeSet<String>) {
    let value = |id: &str| {
        held.iter()
            .find(|(held, _)| held == id)
            .map(|(_, value)| value)
    };
    let mut credentials = Vec::new();
    let mut missing = BTreeSet::new();
    for (id, placement) in stored {
        let signed = placement.hmac.as_deref().map(value);
        let (Some(raw), None | Some(Some(_))) = (value(id), signed) else {
            missing.insert(placement.host.clone());
            continue;
        };
        let mut placed = match signed.flatten() {
            Some(message) => proof(raw.expose(), message.expose()),
            None => raw.expose().to_owned(),
        };
        if let Some(scheme) = &placement.scheme {
            placed = format!("{scheme} {placed}");
        }
        let placed = Secret::new(placed);
        credentials.push(Credential {
            id: format!("stored-input:{id}"),
            host: placement.host.clone(),
            header: placement
                .header
                .as_ref()
                .map(|header| header.to_ascii_lowercase()),
            query: placement.query.clone(),
            value: placed.clone(),
            protected: vec![raw.clone(), placed],
            routes: Routes::Declared(placement.routes.clone()),
        });
    }
    (credentials, missing)
}

/// The header and query names Team's own placements use on `host`.
pub(super) fn owned_fields(credentials: &[Credential], host: &str) -> BTreeSet<String> {
    credentials
        .iter()
        .filter(|credential| credential.host == host)
        .flat_map(|credential| [credential.header.clone(), credential.query.clone()])
        .flatten()
        .collect()
}

/// `urllib.parse.unquote`: `%XX` decoded as UTF-8, invalid sequences replaced.
fn unquote(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let hex = |offset: usize| {
            bytes
                .get(index + offset)
                .and_then(|byte| char::from(*byte).to_digit(16))
        };
        match (bytes[index], hex(1), hex(2)) {
            (b'%', Some(high), Some(low)) => {
                decoded.push(u8::try_from(high * 16 + low).unwrap_or_default());
                index += 3;
            }
            (byte, _, _) => {
                decoded.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

/// Every name the raw query of `target` sets, decoded both as a form (`+` is a space) and as a path would be.
pub(super) fn query_names(target: &str) -> BTreeSet<String> {
    let query = target.split_once('?').map_or("", |(_, query)| query);
    query
        .split('&')
        .flat_map(|pair| {
            let name = pair.split_once('=').map_or(pair, |(name, _)| name);
            [unquote(name), unquote(&name.replace('+', " "))]
        })
        .collect()
}

/// Places every credential of the call's host.
///
/// # Errors
/// A `refused` [`Refusal`] when a value cannot travel in its header.
pub(super) fn inject(call: &Call, credentials: &[Credential]) -> Result<Call, Refusal> {
    let mut placed = call.clone();
    for credential in credentials
        .iter()
        .filter(|credential| credential.host == call.host)
    {
        if let Some(header) = &credential.header {
            if !field_value(credential.value.expose()) {
                return Err(Refusal::new("refused", "credential-value"));
            }
            placed
                .headers
                .push((header.clone(), credential.value.expose().to_owned()));
        } else if let Some(query) = &credential.query {
            let separator = if placed.target.contains('?') {
                '&'
            } else {
                '?'
            };
            let value = crate::failure::percent(credential.value.expose(), false);
            placed.target = format!("{}{separator}{query}={value}", placed.target);
        }
    }
    Ok(placed)
}

/// Refuses a response that carries any protected form in a common encoding, in any header or the body. The body is
/// read as Latin-1 bytes, as UTF-8 text, and with its JSON string escapes decoded, whether or not it parses, so an
/// escaped or duplicated member cannot carry a value past the scan; every comparison ignores case.
pub(super) fn echoes(
    headers: &[(String, String)],
    body: &[u8],
    credentials: &[Credential],
) -> bool {
    let mut forms: BTreeSet<String> = BTreeSet::new();
    for secret in credentials
        .iter()
        .flat_map(|credential| &credential.protected)
    {
        let secret = secret.expose();
        if secret.is_empty() {
            continue;
        }
        forms.insert(lower(secret));
        forms.extend(
            encodings(secret)
                .iter()
                .filter(|form| !form.is_empty())
                .map(|form| lower(form)),
        );
    }
    let text = String::from_utf8_lossy(body);
    let mut texts: Vec<String> = headers
        .iter()
        .map(|(name, value)| lower(&format!("{name}: {value}")))
        .collect();
    texts.push(lower(
        &body
            .iter()
            .map(|byte| char::from(*byte))
            .collect::<String>(),
    ));
    texts.push(lower(&text));
    texts.push(lower(&json_unescaped(&text)));
    forms.iter().any(|form| {
        texts
            .iter()
            .any(|candidate| candidate.contains(form.as_str()))
    })
}

/// Every JSON string escape in `text` decoded; a surrogate pair decodes to its one character, a lone one to U+FFFD.
fn json_unescaped(text: &str) -> String {
    let mut units: Vec<u16> = Vec::with_capacity(text.len());
    let mut characters = text.chars().peekable();
    while let Some(character) = characters.next() {
        if character == '\\' {
            let escaped = match characters.peek() {
                Some('"') => Some(u16::from(b'"')),
                Some('\\') => Some(u16::from(b'\\')),
                Some('/') => Some(u16::from(b'/')),
                Some('b') => Some(8),
                Some('f') => Some(12),
                Some('n') => Some(10),
                Some('r') => Some(13),
                Some('t') => Some(9),
                _ => None,
            };
            if let Some(unit) = escaped {
                characters.next();
                units.push(unit);
                continue;
            }
            let digits: String = characters.clone().skip(1).take(4).collect();
            if characters.peek() == Some(&'u')
                && digits.len() == 4
                && digits.chars().all(|digit| digit.is_ascii_hexdigit())
            {
                units.push(u16::from_str_radix(&digits, 16).unwrap_or_default());
                for _ in 0..5 {
                    characters.next();
                }
                continue;
            }
        }
        let mut buffer = [0_u16; 2];
        units.extend_from_slice(character.encode_utf16(&mut buffer));
    }
    String::from_utf16_lossy(&units)
}
