//! Team's admission of Action effects, verifiers, idempotency, and file inputs through the whole machine contract.

mod common;

use common::{json, object, text};
use strict_json::Value;
use team_assistant::manifest::{
    ManifestContract, canonical_allowed_hosts, canonical_machine_contract,
    canonical_stored_input_declarations,
};
use team_assistant::spec::ActionSpec;

fn names(actions: &[Value], key: &str) -> Vec<String> {
    let mut found: Vec<String> = actions
        .iter()
        .filter_map(|action| match action.get(key) {
            Some(Value::Array(items)) => Some(items.clone()),
            _ => None,
        })
        .flatten()
        .filter_map(|item| match item {
            Value::String(name) => Some(name),
            _ => None,
        })
        .collect();
    found.sort();
    found.dedup();
    found
}

/// Declares every Stored Input the Actions name and every valid idempotency provider as an outbound host, so only
/// the effect rules decide.
fn declared(actions: &[Value]) -> ManifestContract {
    let mut hosts: Vec<String> = actions
        .iter()
        .filter_map(|action| {
            action
                .get("idempotency")
                .and_then(|declaration| declaration.get("provider"))
                .cloned()
        })
        .filter_map(|provider| canonical_allowed_hosts(&Value::Array(vec![provider])).ok())
        .flatten()
        .collect();
    hosts.sort();
    hosts.dedup();
    let entries: Vec<(String, Value)> = names(actions, "stored_inputs")
        .into_iter()
        .map(|name| {
            let placement = [
                ("header", text(&format!("x-{name}"))),
                ("routes", json(r#"[{"method": "POST", "path": "/search"}]"#)),
            ];
            (name, common::stored("Key", &placement))
        })
        .collect();
    let stored_inputs =
        canonical_stored_input_declarations(&entries, &["api.example.com".to_owned()])
            .unwrap_or_default();
    ManifestContract {
        allowed_hosts: hosts,
        integrations: Vec::new(),
        stored_inputs,
    }
}

fn admit_with(
    actions: &Value,
    hosts: Option<Vec<String>>,
) -> Result<Value, team_assistant::ManifestError> {
    let items = match actions {
        Value::Array(items) => items.clone(),
        _ => Vec::new(),
    };
    let mut contract = declared(&items);
    if let Some(hosts) = hosts {
        contract.allowed_hosts = hosts;
    }
    let machine = object(vec![
        ("version", common::int(1)),
        ("actions", actions.clone()),
        ("messages", common::display_messages(&items, &["Key"])),
    ]);
    canonical_machine_contract(
        &machine,
        &contract,
        common::SUMMARY,
        common::ASSISTANT_DESCRIPTION,
    )
}

fn vector_cases(file: &str) -> Vec<Value> {
    match common::protocol(file).get("cases") {
        Some(Value::Array(cases)) => cases.clone(),
        _ => panic!("cases"),
    }
}

#[test]
fn team_admission_matches_every_published_effect_and_input_file_vector() {
    for file in ["vectors/action-effect.json", "vectors/input-file.json"] {
        for case in vector_cases(file) {
            let admitted = admit_with(case.get("actions").expect("actions"), None).is_ok();
            assert_eq!(
                admitted,
                case.get("valid") == Some(&Value::Bool(true)),
                "{file}: {:?}",
                case.get("name")
            );
        }
    }
}

fn idempotency() -> Value {
    json(
        r#"{"provider": "api.example.com", "key": {"location": "header", "name": "Idempotency-Key"}, "scope": "account", "retention_seconds": 86400, "same_payload_required": true}"#,
    )
}

#[test]
fn an_idempotency_provider_must_be_an_exact_outbound_host() {
    let mut action = common::action("run", "mutating");
    common::set(&mut action, "idempotency", idempotency());
    let actions = Value::Array(vec![action]);
    for hosts in [
        vec![],
        vec!["example.com".to_owned()],
        vec!["other.example.com".to_owned(), "api.example.org".to_owned()],
    ] {
        assert!(admit_with(&actions, Some(hosts)).is_err());
    }
    let admitted =
        admit_with(&actions, Some(vec!["api.example.com".to_owned()])).expect("admitted");
    let Some(Value::Array(admitted)) = admitted.get("actions") else {
        panic!("actions")
    };
    let spec = ActionSpec::of(&admitted[0]);
    assert_eq!(
        (spec.effect.as_str(), spec.idempotency),
        ("mutating", Some(idempotency()))
    );
}

fn verified_pair(find_members: &[(&str, Value)]) -> Value {
    let mut create = common::action("create-record", "mutating");
    common::set(
        &mut create,
        "input_schema",
        json(
            r#"{"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": false}"#,
        ),
    );
    let output = json(
        r#"{"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": false}"#,
    );
    common::set(&mut create, "output_schema", output.clone());
    common::set(
        &mut create,
        "verifier",
        json(
            r#"{"action": "find-record", "input": {"operation": {"from": "operation_id"}}, "outcome": "/outcome", "result": "/record"}"#,
        ),
    );
    let mut find = common::action("find-record", "read_only");
    common::set(
        &mut find,
        "input_schema",
        json(
            r#"{"type": "object", "properties": {"operation": {"type": "string"}}, "required": ["operation"], "additionalProperties": false}"#,
        ),
    );
    let outcome =
        json(r#"{"type": "string", "enum": ["occurred", "not_occurred", "inconclusive"]}"#);
    common::set(
        &mut find,
        "output_schema",
        object(vec![
            ("type", text("object")),
            (
                "properties",
                object(vec![("outcome", outcome), ("record", output)]),
            ),
            ("required", json(r#"["outcome"]"#)),
            ("additionalProperties", Value::Bool(false)),
        ]),
    );
    for (key, value) in find_members {
        common::set(&mut find, key, value.clone());
    }
    Value::Array(vec![create, find])
}

#[test]
fn a_verifier_runs_without_a_person() {
    assert!(admit_with(&verified_pair(&[]), None).is_ok());
    for stored_inputs in [r#"["api-key"]"#, r#"["api-key", "api-secret"]"#] {
        let keyed = verified_pair(&[
            ("human_requests", json(r#"["input:password"]"#)),
            ("stored_inputs", json(stored_inputs)),
        ]);
        assert!(admit_with(&keyed, None).is_ok(), "{stored_inputs}");
    }
    for members in [
        vec![("human_requests", json(r#"["approval"]"#))],
        vec![("human_requests", json(r#"["input:password"]"#))],
    ] {
        assert!(admit_with(&verified_pair(&members), None).is_err());
    }
    let interactive = json(
        r#"[{"id": "find-record", "human_requests": ["input:password", "input:text"], "stored_inputs": ["api-key"]}]"#,
    );
    let Value::Array(targets) = interactive else {
        panic!("array")
    };
    assert!(!team_assistant::effect::unattended(&targets[0]));
}

#[test]
fn effect_members_are_required_and_closed() {
    let mut missing = common::action("run", "read_only");
    let Value::Object(members) = &mut missing else {
        panic!("object")
    };
    members.retain(|(name, _)| name != "effect");
    let mut unknown = common::action("run", "read_only");
    common::set(&mut unknown, "retries", common::int(1));
    for action in [missing, unknown, common::action("run", "destructive")] {
        assert!(admit_with(&Value::Array(vec![action]), None).is_err());
    }
}
