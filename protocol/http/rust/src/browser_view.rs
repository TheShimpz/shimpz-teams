//! The Browser screen a Supervisor watches while a chat turn's Browser task runs (`browser_view.py`).
//!
//! Opening a view answers the running task's random `view_id`. A stream request names that id in its path and upgrades
//! to one WebSocket that Team carries to the Browser's display, only while the very task it opened the view for still
//! runs. The upgrade has one exact form: WebSocket version 13, a 16-byte key, the `binary` subprotocol, and no
//! extension.

use std::sync::LazyLock;

use sha1::{Digest, Sha1};
use strict_json::Value;

use crate::python::base64::{self, STANDARD};
use crate::python::regex::{Pattern, declared};
use crate::python::value::{exact_object, field, string};

/// An opened view's id, matched whole.
pub static VIEW_ID_RE: LazyLock<Pattern> = LazyLock::new(|| declared("[0-9a-f]{32}"));
/// The one WebSocket version.
pub const WEBSOCKET_VERSION: &str = "13";
/// The one subprotocol.
pub const SUBPROTOCOL: &str = "binary";
/// A WebSocket key that encodes 16 bytes, matched whole.
pub static KEY_RE: LazyLock<Pattern> = LazyLock::new(|| declared("[A-Za-z0-9+/]{22}=="));
/// RFC 6455's fixed GUID, which the accept value hashes with the key.
pub const ACCEPT_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
/// A view or stream refused because no Browser task runs.
pub const NOT_RUNNING: &str = "browser-not-running";
/// A view or stream refused because another viewer holds it.
pub const BUSY: &str = "browser-view-busy";
/// A view or stream refused because the display is unavailable.
pub const UNAVAILABLE: &str = "browser-view-unavailable";
/// A stream refused for an upgrade outside its exact form.
pub const INVALID_UPGRADE: &str = "browser-view-upgrade-invalid";

/// `canonical_view`: exactly `{"view_id"}` of an opened view, or `None`.
#[must_use]
pub fn canonical_view(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["view_id"])?;
    VIEW_ID_RE
        .is_full_value(field(members, "view_id"))
        .then_some(value)
}

/// `canonical_key`: a WebSocket key that encodes exactly 16 bytes in its canonical text, with no stray low bits.
#[must_use]
pub fn canonical_key(value: &Value) -> Option<&str> {
    let key = string(value).filter(|key| KEY_RE.is_full(key))?;
    let decoded = base64::decode(key, STANDARD)?;
    (base64::encode(&decoded, STANDARD) == key).then_some(key)
}

/// `accept`: the `Sec-WebSocket-Accept` value that answers a canonical `key` (RFC 6455 §4.2.2).
#[must_use]
pub fn accept(key: &str) -> String {
    let digest = Sha1::digest(format!("{key}{ACCEPT_GUID}").as_bytes());
    base64::encode(&digest, STANDARD)
}
