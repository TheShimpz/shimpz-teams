//! `test_routine_pin.py`: the complete Action pin a recorded Routine step holds, and the Assistant scope pin.

use std::collections::BTreeMap;

use strict_json::Value;

use super::{Integration, PinnedAssistant, StoredInput, action_pins, assistant_pin};
use crate::json::{as_str, at, set, text};
use crate::testing::json;

/// The machine contract's Actions of `test_routine_pin.py`, as Python dumps them.
const ACTIONS: &str = r#"[{"id": "create-record", "description": "Run one reviewed test Action.",
  "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": false},
  "output_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": false},
  "integrations": ["cloudflare"], "stored_inputs": [], "input_files": [], "human_requests": ["approval"], "effect": "mutating",
  "idempotency": {"provider": "api.cloudflare.com", "key": {"location": "header", "name": "Idempotency-Key"}, "scope": "account",
    "retention_seconds": 86400, "same_payload_required": true},
  "verifier": {"action": "find-record", "input": {"operation": {"from": "operation_id"}}, "outcome": "/outcome", "result": "/record"}},
 {"id": "find-record", "description": "Run one reviewed test Action.",
  "input_schema": {"type": "object", "properties": {"operation": {"type": "string"}}, "required": ["operation"], "additionalProperties": false},
  "output_schema": {"type": "object", "properties": {"outcome": {"type": "string", "enum": ["occurred", "not_occurred", "inconclusive"]},
    "record": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": false}},
    "required": ["outcome"], "additionalProperties": false},
  "integrations": [], "stored_inputs": ["api-key"], "input_files": [], "human_requests": ["input:password"], "effect": "read_only"},
 {"id": "list-zones", "description": "Run one reviewed test Action.",
  "input_schema": {"type": "object", "properties": {"operation": {"type": "string"}}, "required": ["operation"], "additionalProperties": false},
  "output_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": false},
  "integrations": [], "stored_inputs": [], "input_files": [], "human_requests": [], "effect": "read_only"}]"#;
/// The catalog digest of the fixture's messages, as the Assistant protocol's validator writes it.
const CATALOG: &str = "sha256:c6c71039b8386d019048ddbf2f01d5db6a4b1510a0afb2c7f56ce3df150d406f";
const BRAIN: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";

fn spec() -> PinnedAssistant {
    PinnedAssistant {
        assistant_id: "shimpz-cloudflare".to_owned(),
        version: "1.2.3".to_owned(),
        image: format!("registry.example/cloudflare@sha256:{}", "a".repeat(64)),
        image_labels: vec![
            (
                "org.shimpz.assistant.id".to_owned(),
                "shimpz-cloudflare".to_owned(),
            ),
            (
                "org.shimpz.source.digest".to_owned(),
                format!("sha256:{}", "b".repeat(64)),
            ),
        ],
        allowed_hosts: vec!["api.cloudflare.com".to_owned()],
        actions: crate::json::elements(&json(ACTIONS)).to_vec(),
        integrations: BTreeMap::from([(
            "cloudflare".to_owned(),
            Integration {
                provider: "cloudflare".to_owned(),
                scopes: vec!["dns.edit".to_owned()],
            },
        )]),
        stored_inputs: BTreeMap::from([(
            "api-key".to_owned(),
            StoredInput {
                kind: "password".to_owned(),
                label: "API key".to_owned(),
                description: "Create a test key in the provider dashboard and copy it.".to_owned(),
                help_url: Some("https://dashboard.exa.ai/api-keys".to_owned()),
            },
        )]),
        catalog: CATALOG.to_owned(),
        pack_digest: format!("sha256:{}", "c".repeat(64)),
    }
}

fn pin_of(spec: &PinnedAssistant, action_id: &str) -> String {
    action_pins(spec, [action_id]).expect("pins")[action_id].clone()
}

/// The spec with one member of one Action of its contract replaced.
fn edited(action_id: &str, path: &[&str], value: Value) -> PinnedAssistant {
    let mut changed = spec();
    let action = changed
        .actions
        .iter_mut()
        .find(|action| as_str(at(action, "id")) == Some(action_id))
        .expect("an Action");
    let mut target = action;
    for key in &path[..path.len() - 1] {
        target = match target {
            Value::Object(members) => {
                &mut members
                    .iter_mut()
                    .find(|(name, _)| name == key)
                    .expect("a member")
                    .1
            }
            _ => panic!("an object"),
        };
    }
    set(target, path[path.len() - 1], value);
    changed
}

#[test]
fn the_pin_documents_hash_to_their_recorded_bytes() {
    // A confirmed Routine keeps these pins, so the hashed documents, their fixed locale included, never move.
    let pins = action_pins(&spec(), ["create-record", "list-zones"]).expect("pins");
    assert_eq!(
        pins["create-record"],
        "sha256:5f0ee8e9b69053ebe89b486bec2ae2dab7c82af3505afcf1947cc120d6edfdfa"
    );
    assert_eq!(
        pins["list-zones"],
        "sha256:ad6704d5566801278c0ea2b5e7d7859ad2fe9857cbda7836c410a88477b916f0"
    );
    assert_eq!(
        assistant_pin(&spec(), BRAIN).as_deref(),
        Some("sha256:7ade8a6fc2e87c790f9ceedd915be02c1e79888c7c601183626df23ba2a7dc4f")
    );
    assert_ne!(pin_of(&spec(), "find-record"), pins["create-record"]);
    assert_eq!(action_pins(&spec(), []), Some(BTreeMap::new()));
}

#[test]
fn every_member_that_decides_the_action_is_drift() {
    let baseline = pin_of(&spec(), "create-record");
    let mut drifted = vec![
        edited("create-record", &["output_schema", "title"], text("x")),
        edited("create-record", &["input_schema", "title"], text("x")),
        edited("create-record", &["effect"], text("read_only")),
        edited(
            "create-record",
            &["verifier", "outcome"],
            text("/record/id"),
        ),
        edited("find-record", &["human_requests"], json("[]")),
        edited(
            "create-record",
            &["idempotency", "retention_seconds"],
            json("60"),
        ),
        edited("create-record", &["human_requests"], json("[]")),
        edited("create-record", &["integrations"], json("[]")),
    ];
    let mut catalog = spec();
    catalog.catalog = format!("sha256:{}", "1".repeat(64));
    let mut scopes = spec();
    scopes
        .integrations
        .get_mut("cloudflare")
        .expect("a declaration")
        .scopes = vec!["dns.read".to_owned()];
    let mut stored = spec();
    stored
        .stored_inputs
        .get_mut("api-key")
        .expect("a declaration")
        .label = "Key".to_owned();
    let mut image = spec();
    image.image = format!("registry.example/cloudflare@sha256:{}", "d".repeat(64));
    let mut source = spec();
    source.image_labels = vec![(
        "org.shimpz.source.digest".to_owned(),
        format!("sha256:{}", "e".repeat(64)),
    )];
    let mut version = spec();
    version.version = "1.2.4".to_owned();
    let mut hosts = spec();
    hosts.allowed_hosts.push("www.cloudflare.com".to_owned());
    let mut pack = spec();
    pack.pack_digest = format!("sha256:{}", "f".repeat(64));
    drifted.extend([catalog, scopes, stored, image, source, version, hosts, pack]);
    for changed in drifted {
        assert_ne!(pin_of(&changed, "create-record"), baseline);
    }
}

#[test]
fn an_unrelated_action_leaves_the_pin_but_not_the_assistant_scope() {
    let unrelated = edited("list-zones", &["effect"], text("mutating"));
    assert_eq!(
        pin_of(&unrelated, "create-record"),
        pin_of(&spec(), "create-record")
    );
    let scope = assistant_pin(&spec(), BRAIN).expect("a scope pin");
    assert_ne!(assistant_pin(&unrelated, BRAIN), Some(scope.clone()));
    assert_ne!(
        assistant_pin(&spec(), &format!("sha256:{}", "1".repeat(64))),
        Some(scope)
    );
    let mut unverified = spec();
    if let Value::Object(members) = &mut unverified.actions[0] {
        members.retain(|(name, _)| name != "verifier");
    }
    assert_ne!(
        pin_of(&unverified, "create-record"),
        pin_of(&spec(), "create-record")
    );
}

#[test]
fn the_scope_pin_holds_every_action_pin_byte_for_byte() {
    let pins = action_pins(&spec(), ["create-record", "find-record", "list-zones"]).expect("pins");
    let document = json(&format!(
        r#"{{"actions": {{"create-record": "{}", "find-record": "{}", "list-zones": "{}"}},
            "brain": "{BRAIN}", "format": "shimpz-routine-assistant-pin-v1"}}"#,
        pins["create-record"], pins["find-record"], pins["list-zones"]
    ));
    let expected = format!(
        "sha256:{}",
        crate::json::sha256_hex(&crate::json::canonical(&document))
    );
    assert_eq!(assistant_pin(&spec(), BRAIN), Some(expected));
    let mut undeclared = spec();
    undeclared.stored_inputs.clear();
    assert_eq!(action_pins(&undeclared, ["find-record"]), None);
    assert_eq!(action_pins(&spec(), ["missing"]), None);
}
