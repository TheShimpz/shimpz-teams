//! The OAuth providers Team itself reviews (`integrations/providers.py`).
//!
//! An Assistant names a provider and requests reviewed scopes; it never chooses the provider, its scopes, its API
//! hosts, or the endpoints its bearer reaches. Adding or changing a provider is a controller release.

/// The most scopes one Integration declaration requests.
pub const MAX_REQUESTED_SCOPES: usize = 32;
/// The methods a provider route may name.
pub const METHODS: [&str; 6] = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"];
/// The path segment that matches one 32-hex Cloudflare zone or record identifier.
pub const HEX_ID: &str = "{hex32}";

/// One reviewed provider endpoint: an exact method and a `/`-separated path pattern matched whole, where a
/// [`HEX_ID`] segment stands for exactly 32 lowercase hexadecimal characters and every other segment is literal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Route {
    /// The method.
    pub method: &'static str,
    /// The path pattern.
    pub path: &'static str,
}

/// One reviewed OAuth provider.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Provider {
    /// Its identifier.
    pub id: &'static str,
    /// The API hosts Team sends its bearer to in an Assistant's provider calls (ADR-0106).
    pub api_hosts: &'static [&'static str],
    /// The scopes an Assistant may request, sorted.
    pub allowed_scopes: &'static [&'static str],
    /// The only endpoints the bearer reaches on those hosts (ADR-0106 amendment, 2026-10-09).
    pub routes: &'static [Route],
    /// The provider's public name.
    pub name: &'static str,
    /// The reviewed public summary of what connecting it grants.
    pub summary: &'static str,
}

impl Provider {
    /// Whether one call's method and path (as sent, before any query) are a reviewed endpoint. Percent-encoding,
    /// dot segments, empty segments, and a trailing slash never match: Team refuses rather than normalizes them.
    #[must_use]
    pub fn allows(&self, method: &str, path: &str) -> bool {
        self.routes
            .iter()
            .any(|route| route.method == method && path_matches(route.path, path))
    }

    /// Whether the registry entry is well formed: a canonical id, hosts, valid scopes, and absolute routes with a
    /// reviewed method each.
    #[must_use]
    pub fn valid(&self) -> bool {
        identifier(self.id)
            && !self.api_hosts.is_empty()
            && !self.allowed_scopes.is_empty()
            && !self.routes.is_empty()
            && self.allowed_scopes.iter().all(|scope| scope_text(scope))
            && self
                .routes
                .iter()
                .all(|route| METHODS.contains(&route.method) && route.path.starts_with('/'))
    }
}

fn path_matches(pattern: &str, path: &str) -> bool {
    let mut expected = pattern.split('/');
    let mut actual = path.split('/');
    loop {
        match (expected.next(), actual.next()) {
            (None, None) => return true,
            (Some(HEX_ID), Some(segment)) => {
                if !(segment.len() == 32
                    && segment
                        .bytes()
                        .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')))
                {
                    return false;
                }
            }
            (Some(literal), Some(segment)) if literal == segment => {}
            _ => return false,
        }
    }
}

const ZONE: &str = "/client/v4/zones/{hex32}";
const RECORDS: &str = "/client/v4/zones/{hex32}/dns_records";
const RECORD: &str = "/client/v4/zones/{hex32}/dns_records/{hex32}";

/// Cloudflare. The platform broker holds the client and its secret; Team binds each grant with PKCE S256. The scopes
/// admit zone discovery and DNS-record reads and writes, never a zone or account write; the routes are exactly the
/// calls the reviewed Cloudflare Assistant makes, so no account, user, token, or zone-write endpoint is reachable with
/// the bearer.
pub static CLOUDFLARE: Provider = Provider {
    id: "cloudflare",
    api_hosts: &["api.cloudflare.com"],
    allowed_scopes: &["dns.read", "dns.write", "offline_access", "zone.read"],
    routes: &[
        Route {
            method: "GET",
            path: "/client/v4/zones",
        },
        Route {
            method: "GET",
            path: ZONE,
        },
        Route {
            method: "GET",
            path: RECORDS,
        },
        Route {
            method: "GET",
            path: RECORD,
        },
        Route {
            method: "POST",
            path: RECORDS,
        },
        Route {
            method: "PUT",
            path: RECORD,
        },
        Route {
            method: "DELETE",
            path: RECORD,
        },
    ],
    name: "Cloudflare",
    summary: "Connect your Cloudflare integration so this Assistant can use only its reviewed Cloudflare permissions.",
};

/// Every reviewed provider.
pub static PROVIDERS: [&Provider; 1] = [&CLOUDFLARE];

/// A provider identifier or requested scope set Team does not admit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProviderError;

impl std::fmt::Display for ProviderError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the OAuth provider or its scopes are unavailable")
    }
}

impl std::error::Error for ProviderError {}

/// One reviewed provider and the least-privilege scope set an Integration requests of it, sorted.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Intent {
    /// The provider.
    pub provider: &'static Provider,
    /// The requested scopes, sorted and unique.
    pub scopes: Vec<String>,
}

/// The reviewed provider `id` names.
///
/// # Errors
/// [`ProviderError`] for anything but a reviewed provider's canonical id.
pub fn resolve(id: &str) -> Result<&'static Provider, ProviderError> {
    if !identifier(id) {
        return Err(ProviderError);
    }
    PROVIDERS
        .iter()
        .copied()
        .find(|provider| provider.id == id)
        .ok_or(ProviderError)
}

/// The deterministic intent of one declaration: a reviewed provider and 1 to 32 distinct allowed scopes, sorted.
///
/// # Errors
/// [`ProviderError`] for an unknown provider or any scope that is malformed, repeated, or not allowed.
pub fn intent<S: AsRef<str>>(provider: &str, scopes: &[S]) -> Result<Intent, ProviderError> {
    let provider = resolve(provider)?;
    if !(1..=MAX_REQUESTED_SCOPES).contains(&scopes.len()) {
        return Err(ProviderError);
    }
    let mut sorted: Vec<String> = Vec::with_capacity(scopes.len());
    for scope in scopes {
        let scope = scope.as_ref();
        if !scope_text(scope) || !provider.allowed_scopes.contains(&scope) {
            return Err(ProviderError);
        }
        sorted.push(scope.to_owned());
    }
    sorted.sort_unstable();
    if sorted.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(ProviderError);
    }
    Ok(Intent {
        provider,
        scopes: sorted,
    })
}

/// Whether `text` is a scope: `[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*`.
#[must_use]
pub fn scope_text(text: &str) -> bool {
    let bytes = text.as_bytes();
    if !bytes.first().is_some_and(u8::is_ascii_lowercase) {
        return false;
    }
    let mut after_separator = false;
    for &byte in &bytes[1..] {
        if matches!(byte, b'.' | b'_' | b':' | b'-') {
            if after_separator {
                return false;
            }
            after_separator = true;
        } else if byte.is_ascii_lowercase() || byte.is_ascii_digit() {
            after_separator = false;
        } else {
            return false;
        }
    }
    !after_separator
}

/// Whether `text` is a canonical Integration, provider, or Stored Input identifier.
#[must_use]
pub fn identifier(text: &str) -> bool {
    team_protocol::identifiers::canonical_identifier(&strict_json::Value::String(text.to_owned()))
        .is_some()
}

#[cfg(test)]
mod tests {
    use super::{CLOUDFLARE, PROVIDERS, Provider, Route, intent, resolve, scope_text};

    #[test]
    fn cloudflare_is_the_one_core_owned_provider() {
        let provider = resolve("cloudflare").expect("reviewed");
        assert_eq!(
            provider.allowed_scopes,
            ["dns.read", "dns.write", "offline_access", "zone.read"]
        );
        assert_eq!(PROVIDERS.map(|provider| provider.id), ["cloudflare"]);
        assert!(PROVIDERS.iter().all(|provider| provider.valid()));
        for unknown in ["evil", "Cloudflare", "", "https://evil.example"] {
            assert!(resolve(unknown).is_err());
        }
    }

    #[test]
    fn requested_scopes_are_canonical_and_limited_to_the_trusted_provider() {
        let admitted = intent(
            "cloudflare",
            &["zone.read", "offline_access", "dns.write", "dns.read"],
        )
        .expect("admitted");
        assert_eq!(admitted.provider.id, "cloudflare");
        assert_eq!(
            admitted.scopes,
            ["dns.read", "dns.write", "offline_access", "zone.read"]
        );
        let many: Vec<&str> = vec!["scope"; super::MAX_REQUESTED_SCOPES + 1];
        let refused: [(&str, &[&str]); 7] = [
            ("unknown", &["zone.read"]),
            ("Cloudflare", &["zone.read"]),
            ("cloudflare", &[]),
            ("cloudflare", &["zone.read", "zone.read"]),
            ("cloudflare", &["zone.write"]),
            ("cloudflare", &["zone/read"]),
            ("cloudflare", &many),
        ];
        for (provider, scopes) in refused {
            assert!(intent(provider, scopes).is_err(), "{provider} {scopes:?}");
        }
    }

    #[test]
    fn invalid_registry_metadata_is_refused() {
        const ROUTES: &[Route] = &[Route {
            method: "GET",
            path: "/v1/items",
        }];
        let base = Provider {
            id: "provider",
            api_hosts: &["api.example.com"],
            allowed_scopes: &["data.read"],
            routes: ROUTES,
            name: "Provider",
            summary: "Provider",
        };
        assert!(base.valid());
        let changes: [fn(&mut Provider); 7] = [
            |provider| provider.id = "Provider",
            |provider| provider.api_hosts = &[],
            |provider| provider.allowed_scopes = &[],
            |provider| provider.allowed_scopes = &["bad/scope"],
            |provider| provider.routes = &[],
            |provider| {
                provider.routes = &[Route {
                    method: "TRACE",
                    path: "/v1/items",
                }];
            },
            |provider| {
                provider.routes = &[Route {
                    method: "GET",
                    path: "v1/items",
                }];
            },
        ];
        let invalid = changes.map(|change| {
            let mut provider = base;
            change(&mut provider);
            provider
        });
        for provider in invalid {
            assert!(!provider.valid(), "{provider:?}");
        }
        for scope in ["a", "dns.read", "a1:b2-c3_d4"] {
            assert!(scope_text(scope), "{scope}");
        }
        for scope in ["", "A", "1a", "a.", "a..b", "a/b", "é"] {
            assert!(!scope_text(scope), "{scope}");
        }
    }

    #[test]
    fn the_cloudflare_bearer_reaches_only_the_reviewed_dns_endpoints() {
        let zone = "a".repeat(32);
        let record = "b".repeat(32);
        let allowed = [
            ("GET", "/client/v4/zones".to_owned()),
            ("GET", format!("/client/v4/zones/{zone}")),
            ("GET", format!("/client/v4/zones/{zone}/dns_records")),
            (
                "GET",
                format!("/client/v4/zones/{zone}/dns_records/{record}"),
            ),
            ("POST", format!("/client/v4/zones/{zone}/dns_records")),
            (
                "PUT",
                format!("/client/v4/zones/{zone}/dns_records/{record}"),
            ),
            (
                "DELETE",
                format!("/client/v4/zones/{zone}/dns_records/{record}"),
            ),
        ];
        for (method, path) in &allowed {
            assert!(CLOUDFLARE.allows(method, path), "{method} {path}");
        }
    }

    #[test]
    fn the_cloudflare_bearer_never_reaches_credential_zone_write_or_non_canonical_paths() {
        let zone = "a".repeat(32);
        let record = "b".repeat(32);
        let refused = [
            ("GET", "/client/v4/user/tokens/verify".to_owned()),
            ("POST", "/client/v4/user/tokens".to_owned()),
            ("POST", format!("/client/v4/accounts/{zone}/tokens")),
            ("GET", "/client/v4/user".to_owned()),
            ("GET", "/client/v4/accounts".to_owned()),
            ("PATCH", format!("/client/v4/zones/{zone}")),
            ("DELETE", format!("/client/v4/zones/{zone}")),
            ("POST", "/client/v4/zones".to_owned()),
            (
                "PATCH",
                format!("/client/v4/zones/{zone}/dns_records/{record}"),
            ),
            ("DELETE", format!("/client/v4/zones/{zone}/dns_records")),
            ("GET", "/client/v4/zones/".to_owned()),
            ("GET", "//client/v4/zones".to_owned()),
            ("GET", format!("/client/v4/zones/{zone}/../../user/tokens")),
            ("GET", format!("/client/v4/zones/{zone}/%2e%2e/user")),
            ("GET", format!("/client/v4/zones/{}", zone.to_uppercase())),
            ("GET", format!("/client/v4/zones/{zone}%2Fdns_records")),
            ("GET", "/client/v4/zones/x".to_owned()),
        ];
        for (method, path) in &refused {
            assert!(!CLOUDFLARE.allows(method, path), "{method} {path}");
        }
    }
}
