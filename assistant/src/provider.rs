//! The controller-reviewed OAuth providers an Assistant's Integrations may name (`integrations/providers.py`).
//!
//! An Assistant package may name a provider and request reviewed scopes; it cannot choose the provider, its scopes,
//! its API hosts, or the endpoints that receive its bearer. Adding or changing a provider requires a controller
//! release. Manifest admission reads this registry first, so it lives with Assistant admission; the Integration flow
//! and the provider broker read the same registry.

use strict_json::Value;
use team_protocol::payload::canonical_identifier;

/// The most scopes one Integration requests.
pub const MAX_REQUESTED_SCOPES: usize = 32;

/// One reviewed endpoint path segment.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Segment {
    /// Exactly this text.
    Literal(&'static str),
    /// Exactly 32 lowercase hexadecimal characters (a Cloudflare zone or DNS-record id).
    Hex32,
}

/// One reviewed provider endpoint: an exact method and the segments its canonical path must match whole.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Route {
    /// The method.
    pub method: &'static str,
    /// The path's segments after the leading `/`.
    pub segments: &'static [Segment],
}

/// One reviewed OAuth provider.
#[derive(Debug, PartialEq, Eq)]
pub struct Provider {
    /// Its identifier.
    pub id: &'static str,
    /// The API hosts Team sends its bearer to in an Assistant's provider calls (ADR-0106).
    pub api_hosts: &'static [&'static str],
    /// The scopes an Integration may request.
    pub allowed_scopes: &'static [&'static str],
    /// The only endpoints Team sends its bearer to on its API hosts (ADR-0106 amendment, 2026-10-09).
    pub routes: &'static [Route],
}

impl Provider {
    /// Whether one call's method and path are a reviewed endpoint; a non-canonical path never matches.
    ///
    /// The path is matched as sent, before any query: percent-encoding, dot segments, empty segments, and a trailing
    /// slash never match a reviewed route, so Team refuses rather than normalizes them.
    #[must_use]
    pub fn allows(&self, method: &str, path: &str) -> bool {
        let Some(rest) = path.strip_prefix('/') else {
            return false;
        };
        let parts: Vec<&str> = rest.split('/').collect();
        self.routes.iter().any(|route| {
            route.method == method
                && route.segments.len() == parts.len()
                && route
                    .segments
                    .iter()
                    .zip(&parts)
                    .all(|(segment, part)| match segment {
                        Segment::Literal(text) => text == part,
                        Segment::Hex32 => {
                            part.len() == 32
                                && part.bytes().all(|byte| {
                                    byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
                                })
                        }
                    })
        })
    }
}

use Segment::{Hex32, Literal};

const ZONES: [Segment; 3] = [Literal("client"), Literal("v4"), Literal("zones")];
const ZONE: [Segment; 4] = [ZONES[0], ZONES[1], ZONES[2], Hex32];
const RECORDS: [Segment; 5] = [ZONE[0], ZONE[1], ZONE[2], Hex32, Literal("dns_records")];
const RECORD: [Segment; 6] = [RECORDS[0], RECORDS[1], RECORDS[2], Hex32, RECORDS[4], Hex32];

/// Cloudflare: zone discovery plus reconciliable DNS-record reads and writes; no zone, account, user, or token
/// endpoint is reachable with the bearer, so a workload can never use it to mint or inspect credentials. The platform
/// broker holds the client and its secret; the controller binds each grant with PKCE S256.
pub static CLOUDFLARE: Provider = Provider {
    id: "cloudflare",
    api_hosts: &["api.cloudflare.com"],
    allowed_scopes: &["dns.read", "dns.write", "offline_access", "zone.read"],
    routes: &[
        Route {
            method: "GET",
            segments: &ZONES,
        },
        Route {
            method: "GET",
            segments: &ZONE,
        },
        Route {
            method: "GET",
            segments: &RECORDS,
        },
        Route {
            method: "GET",
            segments: &RECORD,
        },
        Route {
            method: "POST",
            segments: &RECORDS,
        },
        Route {
            method: "PUT",
            segments: &RECORD,
        },
        Route {
            method: "DELETE",
            segments: &RECORD,
        },
    ],
};

/// Every reviewed provider.
pub static PROVIDERS: [&Provider; 1] = [&CLOUDFLARE];

/// An unknown provider or a disallowed OAuth intent.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProviderError;

impl std::fmt::Display for ProviderError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the OAuth provider or its scopes are not reviewed")
    }
}

impl std::error::Error for ProviderError {}

/// Only a controller-reviewed provider identifier.
///
/// # Errors
/// [`ProviderError`] for any other value.
pub fn resolve(provider: &Value) -> Result<&'static Provider, ProviderError> {
    let identifier = canonical_identifier(provider).ok_or(ProviderError)?;
    PROVIDERS
        .iter()
        .copied()
        .find(|candidate| candidate.id == identifier)
        .ok_or(ProviderError)
}

/// `[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*`.
fn scope_grammar(scope: &str) -> bool {
    let bytes = scope.as_bytes();
    bytes.first().is_some_and(u8::is_ascii_lowercase)
        && !bytes
            .last()
            .is_some_and(|byte| matches!(byte, b'.' | b'_' | b':' | b'-'))
        && bytes
            .windows(2)
            .all(|pair| !(is_separator(pair[0]) && is_separator(pair[1])))
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || is_separator(*byte))
}

const fn is_separator(byte: u8) -> bool {
    matches!(byte, b'.' | b'_' | b':' | b'-')
}

/// One deterministic least-privilege scope set for a reviewed provider: 1 to 32 unique reviewed scopes, sorted.
///
/// # Errors
/// [`ProviderError`] for an unknown provider or any other scope list.
pub fn integration_intent(
    provider: &Value,
    requested: &Value,
) -> Result<(&'static Provider, Vec<String>), ProviderError> {
    let provider = resolve(provider)?;
    let Value::Array(requested) = requested else {
        return Err(ProviderError);
    };
    if !(1..=MAX_REQUESTED_SCOPES).contains(&requested.len()) {
        return Err(ProviderError);
    }
    let mut scopes = Vec::with_capacity(requested.len());
    for scope in requested {
        let Value::String(scope) = scope else {
            return Err(ProviderError);
        };
        if !scope_grammar(scope)
            || scopes.contains(scope)
            || !provider.allowed_scopes.contains(&scope.as_str())
        {
            return Err(ProviderError);
        }
        scopes.push(scope.clone());
    }
    scopes.sort();
    Ok((provider, scopes))
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::{CLOUDFLARE, integration_intent, resolve};

    fn parsed(text: &str) -> Value {
        strict_json::parse(text.as_bytes()).expect("JSON")
    }

    #[test]
    fn the_cloudflare_bearer_reaches_only_its_reviewed_endpoints() {
        let zone = "0123456789abcdef0123456789abcdef";
        for (method, path) in [
            ("GET", "/client/v4/zones".to_owned()),
            ("GET", format!("/client/v4/zones/{zone}/dns_records")),
            ("PUT", format!("/client/v4/zones/{zone}/dns_records/{zone}")),
            (
                "DELETE",
                format!("/client/v4/zones/{zone}/dns_records/{zone}"),
            ),
        ] {
            assert!(CLOUDFLARE.allows(method, &path), "{method} {path}");
        }
        for (method, path) in [
            ("GET", "/client/v4/user/tokens/verify".to_owned()),
            ("POST", "/client/v4/user/tokens".to_owned()),
            ("DELETE", format!("/client/v4/zones/{zone}")),
            ("GET", "/client/v4/zones/".to_owned()),
            ("GET", format!("/client/v4/zones/{}", zone.to_uppercase())),
            ("GET", "/client/v4/zones/%30".to_owned()),
            ("GET", "client/v4/zones".to_owned()),
        ] {
            assert!(!CLOUDFLARE.allows(method, &path), "{method} {path}");
        }
    }

    #[test]
    fn an_intent_admits_only_reviewed_unique_scopes_sorted() {
        let provider = parsed(r#""cloudflare""#);
        let (_, scopes) = integration_intent(&provider, &parsed(r#"["zone.read", "dns.read"]"#))
            .expect("reviewed");
        assert_eq!(scopes, ["dns.read", "zone.read"]);
        for scopes in [
            r"[]",
            r#"["dns.read", "dns.read"]"#,
            r#"["account.write"]"#,
            r#""dns.read""#,
            r"[1]",
        ] {
            assert!(
                integration_intent(&provider, &parsed(scopes)).is_err(),
                "{scopes}"
            );
        }
        assert!(resolve(&parsed(r#""github""#)).is_err());
        assert!(resolve(&parsed(r#""Cloudflare""#)).is_err());
    }
}
