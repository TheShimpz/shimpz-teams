//! The reviewed OAuth providers, read from Team's Assistant admission registry (`team_assistant::provider`), and the
//! public copy an Integration challenge and inventory show for each (`integrations/flow.py`).
//!
//! An Assistant names a provider and requests reviewed scopes; it never chooses the provider, its scopes, its API
//! hosts, or the endpoints its bearer reaches.

use strict_json::Value;
pub use team_assistant::provider::{MAX_REQUESTED_SCOPES, PROVIDERS, Provider, ProviderError};

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
    team_assistant::provider::resolve(&Value::String(id.to_owned()))
}

/// The deterministic intent of one declaration: a reviewed provider and 1 to 32 distinct allowed scopes, sorted.
///
/// # Errors
/// [`ProviderError`] for an unknown provider or any scope that is malformed, repeated, or not allowed.
pub fn intent<S: AsRef<str>>(provider: &str, scopes: &[S]) -> Result<Intent, ProviderError> {
    let requested = Value::Array(
        scopes
            .iter()
            .map(|scope| Value::String(scope.as_ref().to_owned()))
            .collect(),
    );
    let (provider, scopes) = team_assistant::provider::integration_intent(
        &Value::String(provider.to_owned()),
        &requested,
    )?;
    Ok(Intent { provider, scopes })
}

/// The reviewed public name and summary a person sees for `provider`; `None` for a provider without reviewed copy.
#[must_use]
pub fn public(provider: &Provider) -> Option<(&'static str, &'static str)> {
    match provider.id {
        "cloudflare" => Some((
            "Cloudflare",
            "Connect your Cloudflare integration so this Assistant can use only its reviewed Cloudflare permissions.",
        )),
        _ => None,
    }
}

/// Whether `text` is a canonical Integration, provider, or Stored Input identifier.
#[must_use]
pub fn identifier(text: &str) -> bool {
    team_protocol::identifiers::canonical_identifier(&Value::String(text.to_owned())).is_some()
}

#[cfg(test)]
mod tests {
    use super::{PROVIDERS, intent, public, resolve};

    #[test]
    fn every_reviewed_provider_has_public_copy() {
        assert!(PROVIDERS.iter().all(|provider| public(provider).is_some()));
        assert_eq!(
            resolve("cloudflare").map(|provider| provider.id),
            Ok("cloudflare")
        );
        assert!(resolve("Cloudflare").is_err() && resolve("https://evil.example").is_err());
    }

    #[test]
    fn requested_scopes_are_admitted_sorted_by_the_shared_registry() {
        let admitted = intent("cloudflare", &["zone.read", "dns.read"]).expect("admitted");
        assert_eq!(admitted.scopes, ["dns.read", "zone.read"]);
        for refused in [&["zone.write"][..], &[], &["zone.read", "zone.read"]] {
            assert!(intent("cloudflare", refused).is_err());
        }
    }
}
