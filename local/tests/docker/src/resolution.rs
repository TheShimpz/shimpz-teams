//! The reference Assistant and the publication the suite seeds for it: the install vector's resolution with the
//! reference Assistant's identity, digests, and the loopback registry reference of its pushed image.

use strict_json::Value;
use team_protocol::python::json::{Options, dumps};
use team_protocol::python::value::{object_of, text};

use crate::api::sha256_hex;
use crate::report::{Failure, Outcome};

/// The reference Assistant's build context: its Dockerfile and the files it copies.
pub const CONTEXT: [(&str, &[u8]); 5] = [
    (
        "Dockerfile",
        include_bytes!("../fixtures/reference-assistant/Dockerfile"),
    ),
    ("shimpz.toml", MANIFEST),
    (
        "shimpz-action",
        include_bytes!("../fixtures/reference-assistant/shimpz-action"),
    ),
    ("shimpz.contract.json", MACHINE_CONTRACT),
    ("shimpz.pack.json", PACK),
];
const MANIFEST: &[u8] = include_bytes!("../fixtures/reference-assistant/shimpz.toml");
const MACHINE_CONTRACT: &[u8] =
    include_bytes!("../fixtures/reference-assistant/shimpz.contract.json");
const PACK: &[u8] = include_bytes!("../fixtures/reference-assistant/shimpz.pack.json");
const VECTORS: &[u8] = include_bytes!("../../../../protocol/install/v1/vectors.json");
/// The icon the seeded publication names (the fixture runner serves exactly these bytes).
const ICON: &[u8] = b"canonical icon";
/// The reference Assistant's id.
pub const ASSISTANT_ID: &str = "shimpz-cloudflare";
const DESCRIPTION: &str = "Browse the zones of your Cloudflare account and read the DNS records of each zone. Every \
                           Action only reads; nothing in your account changes.";
const SITE: &str = "https://shimpz.com/";
const GITHUB: &str = "https://github.com/TheShimpz";
/// Python's `json.dumps(value, separators=(",", ":"))`.
const SEEDED_JSON: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};

/// The publication's source digest: SHA-256 over the manifest and the machine contract.
pub fn source_digest() -> String {
    format!(
        "sha256:{}",
        sha256_hex(&[MANIFEST, MACHINE_CONTRACT].concat())
    )
}

fn digest(raw: &[u8]) -> Value {
    text(&format!("sha256:{}", sha256_hex(raw)))
}

/// The seeded publication's bytes for the image pushed as `trusted_reference` (`repository@sha256:…`).
pub fn seeded(trusted_reference: &str) -> Outcome<Vec<u8>> {
    let vectors = strict_json::parse(VECTORS)
        .map_err(|_| Failure("the install vectors are not JSON".into()))?;
    let base = crate::json::at(&vectors, &["fixtures", "resolve_response", "value"])
        .cloned()
        .ok_or_else(|| Failure("the install vectors have no resolve_response fixture".into()))?;
    let contract = strict_json::parse(MACHINE_CONTRACT)
        .map_err(|_| Failure("the contract is not JSON".into()))?;
    let oci_digest = trusted_reference
        .rsplit_once('@')
        .map(|(_, digest)| digest)
        .ok_or_else(|| Failure(format!("{trusted_reference} is not a digest reference")))?;
    let updates = updates(trusted_reference, oci_digest, contract);
    Ok(dumps(&updated(base, updates), SEEDED_JSON).into_bytes())
}

/// The reference Assistant's members of its publication.
fn updates<'a>(
    trusted_reference: &str,
    oci_digest: &str,
    contract: Value,
) -> Vec<(&'a str, Value)> {
    vec![
        ("assistant_id", text(ASSISTANT_ID)),
        ("name", text("Shimpz Cloudflare")),
        (
            "summary",
            text("List Cloudflare zones and inspect their DNS records through OAuth."),
        ),
        ("description", text(DESCRIPTION)),
        (
            "links",
            object_of(vec![("site", text(SITE)), ("github", text(GITHUB))]),
        ),
        ("assistant_version", text("0.1.0")),
        ("creators", Value::Array(vec![text("@roxygens")])),
        ("source_digest", text(&source_digest())),
        ("image_reference", text(trusted_reference)),
        ("oci_digest", text(oci_digest)),
        ("manifest_digest", digest(MANIFEST)),
        ("machine_contract_digest", digest(MACHINE_CONTRACT)),
        ("icon_digest", digest(ICON)),
        ("pack_digest", digest(PACK)),
        ("machine_contract", contract),
        (
            "allowed_hosts",
            Value::Array(vec![text("api.cloudflare.com")]),
        ),
        (
            "integrations",
            Value::Array(vec![object_of(vec![
                ("id", text("cloudflare")),
                ("provider", text("cloudflare")),
                (
                    "scopes",
                    Value::Array(vec![
                        text("dns.read"),
                        text("offline_access"),
                        text("zone.read"),
                    ]),
                ),
            ])]),
        ),
        ("stored_inputs", Value::Array(Vec::new())),
    ]
}

/// Python's `dict.update`: an existing member keeps its place, a new one is appended.
fn updated(base: Value, updates: Vec<(&str, Value)>) -> Value {
    let mut members = match base {
        Value::Object(members) => members,
        _ => Vec::new(),
    };
    for (name, value) in updates {
        match members.iter_mut().find(|(key, _)| key == name) {
            Some(member) => member.1 = value,
            None => members.push((name.to_owned(), value)),
        }
    }
    Value::Object(members)
}

#[cfg(test)]
mod tests {
    use super::{DESCRIPTION, GITHUB, MANIFEST, SITE, seeded, source_digest};

    #[test]
    fn the_seed_carries_the_manifest_s_presentation_and_the_pushed_digest() {
        let manifest = String::from_utf8(MANIFEST.to_vec()).expect("text");
        assert!(manifest.contains(&format!("description = \"{DESCRIPTION}\"")));
        assert!(manifest.contains(&format!("site = \"{SITE}\"")));
        assert!(manifest.contains(&format!("github = \"{GITHUB}\"\n")));
        let reference = format!(
            "127.0.0.1:5000/shimpz/shimpz-cloudflare@sha256:{}",
            "a".repeat(64)
        );
        let raw = seeded(&reference).expect("seeded");
        let document = strict_json::parse(&raw).expect("json");
        let digest = format!("sha256:{}", "a".repeat(64));
        assert_eq!(
            crate::json::text(&document, &["oci_digest"]),
            Some(digest.as_str())
        );
        assert_eq!(
            crate::json::text(&document, &["source_digest"]),
            Some(source_digest().as_str())
        );
        assert_eq!(crate::json::integer(&document, &["version"]), Some(1));
        assert_eq!(crate::json::items(&document, &["stored_inputs"]), []);
        let referenced: Vec<&str> = crate::json::items(&document, &["machine_contract", "actions"])
            .iter()
            .flat_map(|action| crate::json::strings(action, &["stored_inputs"]))
            .collect();
        assert_eq!(referenced, Vec::<&str>::new());
        assert!(
            !String::from_utf8_lossy(&raw)
                .to_lowercase()
                .contains("whatsapp")
        );
    }
}
