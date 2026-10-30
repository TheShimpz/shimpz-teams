//! Team's independent admission against the published Assistant protocol vectors: every manifest, every Stored
//! Input route declaration, and every Action schema in both positions.

mod common;

use strict_json::Value;
use team_assistant::manifest::{
    ManifestContract, canonical_machine_contract, canonical_stored_input_declarations,
    parse_manifest_contract, parse_manifest_genesis,
};

fn cases(file: &str) -> Vec<Value> {
    match common::protocol(file).get("cases") {
        Some(Value::Array(cases)) if !cases.is_empty() => cases.clone(),
        _ => panic!("{file} holds cases"),
    }
}

fn name(case: &Value) -> &str {
    match case.get("name") {
        Some(Value::String(name)) => name,
        _ => panic!("name"),
    }
}

fn valid(case: &Value) -> bool {
    case.get("valid") == Some(&Value::Bool(true))
}

#[test]
fn every_published_manifest_vector_agrees() {
    for case in cases("vectors/manifest.json") {
        let Some(Value::String(manifest)) = case.get("manifest") else {
            panic!("manifest")
        };
        let admitted = parse_manifest_contract(manifest.as_bytes()).is_ok()
            && parse_manifest_genesis(manifest.as_bytes()).is_ok();
        assert_eq!(admitted, valid(&case), "{}", name(&case));
    }
}

#[test]
fn every_published_route_vector_agrees_and_keeps_the_routes_as_declared() {
    let hosts = vec!["api.example.com".to_owned()];
    for case in cases("vectors/route.json") {
        let routes = case.get("routes").expect("routes").clone();
        let declaration = common::stored("Token", &[("routes", routes.clone())]);
        let admitted =
            canonical_stored_input_declarations(&[("token".to_owned(), declaration)], &hosts);
        assert_eq!(admitted.is_ok(), valid(&case), "{}", name(&case));
        if let Ok(admitted) = admitted {
            assert_eq!(admitted[0].routes, routes, "{}", name(&case));
        }
    }
}

#[test]
fn every_published_action_schema_vector_agrees_in_both_positions() {
    let empty = ManifestContract {
        allowed_hosts: Vec::new(),
        integrations: Vec::new(),
        stored_inputs: Vec::new(),
    };
    for case in cases("vectors/action-schema.json") {
        for position in ["input_schema", "output_schema"] {
            let mut action = common::action("run", "read_only");
            common::set(
                &mut action,
                position,
                case.get("schema").expect("schema").clone(),
            );
            let contract = common::contract(vec![action], &[]);
            let admitted = canonical_machine_contract(
                &contract,
                &empty,
                common::SUMMARY,
                common::ASSISTANT_DESCRIPTION,
            )
            .is_ok();
            assert_eq!(admitted, valid(&case), "{} as {position}", name(&case));
        }
    }
}
