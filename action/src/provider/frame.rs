//! Admission of one closed `fetch` frame for one of the Assistant's reviewed hosts.

use strict_json::{Number, Value};

use super::{
    Call, MAX_HEADER_BYTES, MAX_HEADERS, MAX_REQUEST_BYTES, MAX_URL_CHARACTERS, METHODS, Refusal,
};

/// Headers Team owns on every provider call; an Action never sets them.
pub const RESERVED_HEADERS: [&str; 12] = [
    "accept-encoding",
    "connection",
    "content-length",
    "expect",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];
const FRAME_FIELDS: [&str; 6] = ["type", "method", "url", "headers", "body", "timeout_ms"];

fn refused(reason: &'static str) -> Refusal {
    Refusal::new("refused", reason)
}

/// Admits one frame: exactly its closed fields, a reviewed method, an https URL to one of `hosts`, bounded headers
/// Team does not own, an optional bounded base64 body, and an optional timeout of 1 to 30,000 ms.
///
/// # Errors
/// A `refused` [`Refusal`] naming the first rule broken.
pub fn parse(frame: &Value, hosts: &[String]) -> Result<Call, Refusal> {
    let Value::Object(members) = frame else {
        return Err(refused("frame"));
    };
    let has = |key: &str| members.iter().any(|(name, _)| name == key);
    let text = |key: &str| match frame.get(key) {
        Some(Value::String(text)) => Some(text.as_str()),
        _ => None,
    };
    let shaped = text("type") == Some("fetch")
        && ["type", "method", "url", "headers"]
            .iter()
            .all(|key| has(key))
        && members
            .iter()
            .all(|(name, _)| FRAME_FIELDS.contains(&name.as_str()));
    let method = text("method").filter(|method| METHODS.contains(method));
    let (Some(method), true) = (method, shaped) else {
        return Err(refused("frame"));
    };
    let (host, target) = url(frame.get("url"), hosts)?;
    let timeout = match frame.get("timeout_ms") {
        None | Some(Value::Null) => None,
        Some(Value::Number(Number::Int(milliseconds))) if (1..=30_000).contains(milliseconds) => {
            Some(u64::try_from(*milliseconds).unwrap_or_default())
        }
        Some(_) => return Err(refused("timeout")),
    };
    Ok(Call {
        method: method.to_owned(),
        host,
        target,
        headers: headers(frame.get("headers"))?,
        body: body(frame.get("body"))?,
        timeout_ms: timeout,
    })
}

/// The host and request target of an `https://<reviewed host>/...` URL: no userinfo, port, fragment, or other host;
/// the target is the path (`/` when empty) and any non-empty raw query.
fn url(value: Option<&Value>, hosts: &[String]) -> Result<(String, String), Refusal> {
    let Some(Value::String(url)) = value else {
        return Err(refused("url"));
    };
    let length = url.chars().count();
    if !(9..=MAX_URL_CHARACTERS).contains(&length)
        || !url.bytes().all(|byte| byte > b' ' && byte < 0x7f)
    {
        return Err(refused("url"));
    }
    let Some(rest) = url.strip_prefix("https://") else {
        return Err(refused("host"));
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, rest) = rest.split_at(end);
    if url.contains('#') || !hosts.iter().any(|host| host == authority) {
        return Err(refused("host"));
    }
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    let path = if path.is_empty() { "/" } else { path };
    let target = if query.is_empty() {
        path.to_owned()
    } else {
        format!("{path}?{query}")
    };
    Ok((authority.to_owned(), target))
}

fn token_name(name: &str) -> bool {
    (1..=64).contains(&name.len())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
}

/// A Latin-1 header value without a line break or any control character other than tab.
#[must_use]
pub fn field_value(value: &str) -> bool {
    value.chars().all(|character| {
        character == '\t' || (' '..='\u{ff}').contains(&character) && character != '\u{7f}'
    })
}

fn headers(value: Option<&Value>) -> Result<Vec<(String, String)>, Refusal> {
    let Some(Value::Array(items)) = value else {
        return Err(refused("headers"));
    };
    if items.len() > MAX_HEADERS {
        return Err(refused("headers"));
    }
    let mut headers: Vec<(String, String)> = Vec::with_capacity(items.len());
    for item in items {
        let pair = match item {
            Value::Array(pair) => pair.as_slice(),
            _ => &[],
        };
        let [Value::String(name), Value::String(field)] = pair else {
            return Err(refused("headers"));
        };
        if !token_name(name) || !field_value(field) {
            return Err(refused("headers"));
        }
        let name = name.to_ascii_lowercase();
        if RESERVED_HEADERS.contains(&name.as_str()) {
            return Err(refused("reserved-header"));
        }
        if headers.iter().any(|(seen, _)| *seen == name) {
            return Err(refused("headers"));
        }
        headers.push((name, field.clone()));
    }
    let size: usize = headers
        .iter()
        .map(|(name, field)| name.len() + field.chars().count())
        .sum();
    if size > MAX_HEADER_BYTES {
        return Err(refused("headers"));
    }
    Ok(headers)
}

fn body(value: Option<&Value>) -> Result<Option<Vec<u8>>, Refusal> {
    let encoded = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::String(encoded)) if encoded.len() <= MAX_REQUEST_BYTES.div_ceil(3) * 4 => {
            encoded
        }
        Some(_) => return Err(refused("body")),
    };
    let decoded = team_storage::envelope::decode_base64(encoded).map_err(|_| refused("body"))?;
    if decoded.len() > MAX_REQUEST_BYTES {
        return Err(refused("body"));
    }
    Ok(Some(decoded))
}
