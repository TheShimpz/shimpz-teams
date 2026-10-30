//! Whether text looks like credential material: Team's Assistant-manifest predicate (`resembles_credential` of
//! `assistant/manifest.py`), which the Routine logic applies to every literal, kept value, and shown result.

use std::sync::LazyLock;

use team_protocol::python::regex::{Pattern, declared};
use team_protocol::python::text::strip;

use crate::pattern::{Folded, fold_dotted_i};

/// A bearer token, an assigned secret, or a provider key, in Python's `(?i)` syntax without its flag.
static SECRET_VALUE: LazyLock<Folded> = LazyLock::new(|| {
    Folded::declared(concat!(
        r"(?:bearer\s+[a-z0-9._~-]{12,}|(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)",
        r"\s*[:=]\s*\S+|(?:sk|ghp|github_pat|glpat|xox[baprs])[-_][a-z0-9_-]{12,})",
    ))
});
/// A JWT, matched whole.
static JWT: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\Z"));

/// Whether text looks like credential material: a bearer token, an assigned secret, a provider key, or a JWT.
///
/// A search that cannot complete counts as credential material, which refuses or redacts.
#[must_use]
pub fn resembles_credential(value: &str) -> bool {
    // Python's case-insensitive `i` and `[a-z]` also read U+0130 and U+0131, which the pattern's engine does not.
    SECRET_VALUE.found(&fold_dotted_i(value)).unwrap_or(true)
        || JWT.fullmatch(strip(value)).unwrap_or(true)
}

#[cfg(test)]
mod tests {
    use super::resembles_credential;

    #[test]
    fn credential_shapes_are_recognized_in_any_case() {
        for credential in [
            "Bearer abcdefghijklmnop",
            "BEARER ABCDEFGHIJKLMNOP",
            "api_key=abc",
            "ApI-KeY : x",
            "apİ_key=abc",
            "access_token=1",
            "client-secret: s",
            "password=hunter2",
            "sk-abcdefghijklmnop",
            "ghp_abcdefghijklmnop",
            "xoxb-abcdefghijklmnop",
            "eyJhbGciOiJIUzI1.eyJzdWIiOjEyMzQ1.c2lnbmF0dXJlMTIz",
            " eyJhbGciOiJIUzI1.eyJzdWIiOjEyMzQ1.c2lnbmF0dXJlMTIz\n",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        ] {
            assert!(resembles_credential(credential), "{credential}");
        }
        for plain in [
            "plain",
            "Bearer short",
            "sk-short",
            "api key",
            "the password is long",
            "eyJhbGci.short.x",
            "zone-123",
        ] {
            assert!(!resembles_credential(plain), "{plain}");
        }
    }
}
