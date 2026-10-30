//! A binding's runtime contract, its language pack, and the Assistant page it projects, from the reference Assistant.

mod common;

use assistant_protocol::catalog::{canonical_json, pack_digest};
use common::{json, object, text};
use strict_json::Value;
use team_assistant::details::AssistantPage;
use team_assistant::language::{admit_pack, catalog_digest, require_binding};
use team_assistant::manifest::{canonical_machine_contract, parse_manifest_contract};
use team_assistant::spec::{Direction, action_summary, digest_is_bound, runtime_contract};

fn machine_contract() -> Value {
    let declared = parse_manifest_contract(&common::fixture("shimpz.toml")).expect("manifest");
    let raw = strict_json::parse(&common::fixture("shimpz.contract.json")).expect("JSON");
    canonical_machine_contract(
        &raw,
        &declared,
        common::REFERENCE_SUMMARY,
        common::REFERENCE_DESCRIPTION,
    )
    .expect("contract")
}

fn binding_document() -> Value {
    object(vec![
        (
            "integrations",
            json(
                r#"[{"id": "cloudflare", "provider": "cloudflare", "scopes": ["dns.read", "offline_access", "zone.read"]}]"#,
            ),
        ),
        ("allowed_hosts", json(r#"["api.cloudflare.com"]"#)),
        ("stored_inputs", Value::Array(Vec::new())),
        ("description", text(common::REFERENCE_DESCRIPTION)),
        (
            "links",
            json(r#"{"site": "https://shimpz.com/", "github": "https://github.com/TheShimpz"}"#),
        ),
        ("summary", text(common::REFERENCE_SUMMARY)),
        ("machine_contract", machine_contract()),
    ])
}

#[test]
fn a_binding_document_admits_its_exactly_canonical_runtime_contract() {
    let runtime = runtime_contract(&binding_document()).expect("admitted");
    assert_eq!(runtime.allowed_hosts, ["api.cloudflare.com"]);
    let zones = runtime.action("list-zones").expect("declared Action");
    assert_eq!(
        (zones.summary.as_str(), zones.effect.as_str()),
        ("List zones", "read_only")
    );
    assert!(zones.validate(Direction::Input, &json("[]")).is_err());
    let mut reordered = binding_document();
    let Some(Value::Object(_)) = reordered.get("machine_contract") else {
        panic!("contract")
    };
    let mut contract = machine_contract();
    if let Value::Object(members) = &mut contract
        && let Some((_, Value::Array(actions))) =
            members.iter_mut().find(|(name, _)| name == "actions")
    {
        actions.reverse();
    }
    common::set(&mut reordered, "machine_contract", contract);
    assert!(runtime_contract(&reordered).is_err());
    let mut foreign = binding_document();
    common::set(
        &mut foreign,
        "integrations",
        json(r#"[{"id": "cloudflare", "provider": "github", "scopes": ["dns.read"]}]"#),
    );
    assert!(runtime_contract(&foreign).is_err());
    let mut widened = binding_document();
    common::set(
        &mut widened,
        "allowed_hosts",
        json(r#"["api.cloudflare.com", "localhost"]"#),
    );
    assert!(runtime_contract(&widened).is_err());
}

fn reference_pack() -> (Value, Vec<u8>) {
    (machine_contract(), common::fixture("shimpz.pack.json"))
}

#[test]
fn the_image_pack_is_admitted_only_for_its_exact_catalog_and_digest() {
    let (contract, raw) = reference_pack();
    let messages = contract.get("messages").expect("messages");
    let digest = pack_digest(&raw);
    let zero = format!("sha256:{}", "0".repeat(64));
    let pack = admit_pack(&raw, messages, &digest).expect("the reference pack is admitted");
    assert_eq!(pack.catalog_digest, catalog_digest(&contract));
    assert!(require_binding(&pack, &contract, &digest).is_ok());
    assert!(require_binding(&pack, &contract, &zero).is_err());
    assert!(admit_pack(&raw, messages, &zero).is_err());
    assert!(admit_pack(&raw, messages, "sha256:short").is_err());
}

#[test]
fn a_tampered_or_incomplete_pack_is_refused_under_its_own_digest() {
    let (contract, raw) = reference_pack();
    let messages = contract.get("messages").expect("messages");
    let mut tampered = raw.clone();
    let position = tampered
        .iter()
        .rposition(u8::is_ascii_lowercase)
        .expect("a letter");
    tampered[position] = tampered[position].to_ascii_uppercase();
    assert!(admit_pack(&tampered, messages, &pack_digest(&tampered)).is_err());
    let mut incomplete = strict_json::parse(&raw).expect("JSON");
    if let Value::Object(members) = &mut incomplete
        && let Some((_, Value::Object(locales))) =
            members.iter_mut().find(|(name, _)| name == "locales")
        && let Some((_, Value::Object(entries))) = locales.first_mut()
    {
        entries.pop();
    }
    let incomplete = canonical_json(&incomplete);
    assert!(admit_pack(&incomplete, messages, &pack_digest(&incomplete)).is_err());
}

fn page() -> AssistantPage {
    AssistantPage {
        assistant_id: "shimpz-cloudflare".to_owned(),
        version: "0.1.0".to_owned(),
        name: "Shimpz Cloudflare".to_owned(),
        creators: vec!["@roxygens".to_owned()],
        summary: common::REFERENCE_SUMMARY.to_owned(),
        description: common::REFERENCE_DESCRIPTION.to_owned(),
        links: json(r#"{"site": "https://shimpz.com/", "github": "https://github.com/TheShimpz"}"#),
        machine_contract: machine_contract(),
        integrations: vec![("cloudflare".to_owned(), "cloudflare".to_owned())],
        stored_inputs: Vec::new(),
    }
}

#[test]
fn english_is_the_catalog_and_any_other_language_its_pack_translation() {
    let contract = machine_contract();
    let raw = common::fixture("shimpz.pack.json");
    let pack = admit_pack(
        &raw,
        contract.get("messages").expect("messages"),
        &pack_digest(&raw),
    )
    .expect("pack");
    let english = page().localized("en", None).expect("English");
    assert_eq!(
        english.get("summary"),
        Some(&text(common::REFERENCE_SUMMARY))
    );
    let german = page().localized("de", Some(&pack)).expect("German");
    let translated = pack
        .template(
            &assistant_protocol::catalog::message_id(common::REFERENCE_SUMMARY),
            "de",
        )
        .expect("translation");
    assert_eq!(german.get("summary"), Some(&text(&translated)));
    assert_ne!(translated, common::REFERENCE_SUMMARY);
    assert!(page().localized("de", None).is_err());
    let mut outside = page();
    outside.name = String::new();
    assert!(outside.localized("en", None).is_err());
}

#[test]
fn ids_read_as_sentences_and_only_a_real_digest_is_bound() {
    assert_eq!(action_summary("list-dns-records"), "List dns records");
    assert!(digest_is_bound(&format!(
        "ghcr.io/theshimpz/shimpz-assistant@sha256:{}",
        "a".repeat(64)
    )));
    assert!(!digest_is_bound(&format!(
        "ghcr.io/theshimpz/shimpz-assistant@sha256:{}",
        "0".repeat(64)
    )));
}
