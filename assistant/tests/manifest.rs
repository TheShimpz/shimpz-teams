//! Manifest and machine-contract admission: the consequences Team relies on, ported from the Python suite.

mod common;

use common::{contract, json, manifest, stored, text};
use strict_json::Value;
use team_assistant::manifest::{
    IntegrationDeclaration, ManifestContract, automatic_update_preserves_egress,
    canonical_allowed_hosts, canonical_integration_declarations, canonical_machine_contract,
    canonical_manifest_contract, canonical_stored_input_declarations, parse_machine_contract,
    parse_manifest_contract, parse_manifest_genesis, parse_manifest_identity,
    parse_manifest_presentation, reviewed_manifest_contract,
};

fn hosts(names: &[&str]) -> Vec<String> {
    names.iter().map(|name| (*name).to_owned()).collect()
}

fn declarations(entries: Vec<(&str, Value)>) -> Vec<(String, Value)> {
    entries
        .into_iter()
        .map(|(id, value)| (id.to_owned(), value))
        .collect()
}

fn empty() -> ManifestContract {
    ManifestContract {
        allowed_hosts: Vec::new(),
        integrations: Vec::new(),
        stored_inputs: Vec::new(),
    }
}

fn admit(
    machine: &Value,
    declared: &ManifestContract,
) -> Result<Value, team_assistant::ManifestError> {
    canonical_machine_contract(
        machine,
        declared,
        common::SUMMARY,
        common::ASSISTANT_DESCRIPTION,
    )
}

fn reference_integrations() -> Vec<IntegrationDeclaration> {
    let scopes = json(r#"["zone.read", "dns.read", "offline_access"]"#);
    canonical_integration_declarations(&declarations(vec![("cloudflare", scopes)]))
        .expect("reviewed")
}

#[test]
fn the_reference_manifest_matches_its_reviewed_cloudflare_intent() {
    let declared = parse_manifest_contract(&common::fixture("shimpz.toml")).expect("admitted");
    let integrations = reference_integrations();
    let scopes = ["dns.read", "offline_access", "zone.read"]
        .map(str::to_owned)
        .to_vec();
    let expected = IntegrationDeclaration {
        id: "cloudflare".to_owned(),
        provider: "cloudflare".to_owned(),
        scopes,
    };
    assert_eq!(integrations, [expected]);
    let hosts = json(r#"["api.cloudflare.com"]"#);
    let reviewed = reviewed_manifest_contract(&hosts, &integrations, &[]).expect("reviewed");
    assert_eq!(declared, reviewed);
}

#[test]
fn the_reference_contract_identity_and_copy_are_admitted() {
    let raw = common::fixture("shimpz.toml");
    let declared = parse_manifest_contract(&raw).expect("admitted");
    let (summary, description) = (common::REFERENCE_SUMMARY, common::REFERENCE_DESCRIPTION);
    let raw_contract = common::fixture("shimpz.contract.json");
    let machine = strict_json::parse(&raw_contract).expect("JSON");
    let canonical = canonical_machine_contract(&machine, &declared, summary, description);
    assert!(canonical.is_ok());
    assert_eq!(
        parse_machine_contract(&raw_contract, &declared, summary, description),
        canonical
    );
    let identity = parse_manifest_identity(&raw).expect("identity");
    assert_eq!(
        (identity.assistant_id.as_str(), identity.version.as_str()),
        ("shimpz-cloudflare", "0.1.0")
    );
    assert_eq!(
        parse_manifest_presentation(&raw)
            .expect("presentation")
            .description,
        description
    );
    assert!(
        parse_manifest_genesis(&raw)
            .expect("Genesis")
            .starts_with("Use this Assistant only")
    );
}

#[test]
fn the_summary_is_at_most_eighty_characters() {
    for summary in ["s".repeat(80), format!("{}\u{1f44b}", "s".repeat(79))] {
        assert!(parse_manifest_contract(&manifest(&["api.example.com"], &summary, "")).is_ok());
    }
    assert!(parse_manifest_contract(&manifest(&["api.example.com"], &"s".repeat(81), "")).is_err());
}

const APPENDED: [&str; 8] = [
    "unexpected = 2\n",
    "[unexpected.lookup]\nname = \"Lookup\"\n",
    "[integrations.cloudflare]\nunexpected = \"x\"\nscopes = [\"zone.read\"]\n",
    "[integrations.github]\nscopes = [\"repo.read\"]\n",
    "[integrations.cloudflare]\nscopes = [\"zone.write\"]\n",
    "[integrations.cloudflare]\nscopes = []\n",
    "homepage = \"https://example.com\"\n",
    "access_token = \"credential-value-123456\"\n",
];
const REPLACED: [(&str, &str); 10] = [
    ("\n[network]\n", "\n"),
    ("spec = 1", "spec = 4"),
    ("id = \"fixture-assistant\"", "id = \"Invalid\""),
    ("id = \"fixture-assistant\"", "id = \"postgres\""),
    ("version = \"0.1.0\"", "version = \"v1\""),
    ("version = \"0.1.0\"", "version = 1979-05-27"),
    ("genesis = \"Use the available Actions.\"", "genesis = \"\""),
    ("creators = [\"@fixture\"]", "creators = []"),
    ("creators = [\"@fixture\"]", "creators = [\"fixture\"]"),
    (
        "https://github.com/TheShimpz/",
        "http://github.com/TheShimpz/",
    ),
];

fn fixture_manifest() -> String {
    String::from_utf8(manifest(
        &["api.example.com"],
        "Exercise immutable admission.",
        "",
    ))
    .expect("utf-8")
}

#[test]
fn unsupported_fields_metadata_and_credential_material_fail_closed() {
    let mut refused: Vec<Vec<u8>> = APPENDED
        .iter()
        .map(|extra| format!("{}{extra}", fixture_manifest()).into_bytes())
        .collect();
    refused.extend(
        REPLACED
            .iter()
            .map(|(from, to)| fixture_manifest().replacen(from, to, 1).into_bytes()),
    );
    refused.extend([
        format!("{}\0", fixture_manifest()).into_bytes(),
        b"\xff".to_vec(),
        Vec::new(),
        b"cloudflare".repeat(26_215),
    ]);
    for raw in refused {
        assert!(
            parse_manifest_contract(&raw).is_err(),
            "{}",
            String::from_utf8_lossy(&raw[..raw.len().min(300)])
        );
    }
}

#[test]
fn only_exact_public_hosts_are_outbound_hosts() {
    let unsafe_hosts = [
        "*.example.com",
        "https://example.com",
        "example.com:443",
        "127.0.0.1",
        "localhost",
        "Example.com",
        "example.com.",
        "example..com",
        "t\u{e9}st.example",
        "api.example.test",
    ];
    for host in unsafe_hosts {
        assert!(
            canonical_allowed_hosts(&Value::Array(vec![text(host)])).is_err(),
            "{host}"
        );
    }
    let sorted = canonical_allowed_hosts(&json(r#"["b.example.com", "a.example.com"]"#));
    assert_eq!(sorted, Ok(hosts(&["a.example.com", "b.example.com"])));
    assert!(canonical_allowed_hosts(&json(r#"["a.example.com", "a.example.com"]"#)).is_err());
}

#[test]
fn a_spec_python_reads_as_one_is_admitted() {
    let raw = String::from_utf8(manifest(
        &["api.example.com"],
        "Exercise immutable admission.",
        "",
    ))
    .expect("utf-8");
    assert!(parse_manifest_contract(raw.replacen("spec = 1", "spec = 1.0", 1).as_bytes()).is_ok());
}

#[test]
fn a_stored_input_admits_only_declarative_metadata_and_one_owned_placement() {
    let raw = manifest(
        &["api.example.com"],
        "Exercise immutable admission.",
        &format!(
            "[stored_inputs.whatsapp-token]\nkind = \"password\"\nlabel = \"WhatsApp token\"\ndescription = \"{}\"\nhelp_url = \"{}\"\nhost = \"api.example.com\"\nheader = \"Authorization\"\nscheme = \"Bearer\"\nroutes = [{{ method = \"POST\", path = \"/v23.0/*/messages\" }}]\n",
            common::STORED_INPUT_HELP,
            common::HELP_URL
        ),
    );
    let parsed = parse_manifest_contract(&raw).expect("admitted");
    let declaration = &parsed.stored_inputs[0];
    assert_eq!(
        (
            declaration.id.as_str(),
            declaration.header.as_deref(),
            declaration.scheme.as_deref()
        ),
        ("whatsapp-token", Some("Authorization"), Some("Bearer"))
    );
    assert_eq!(
        declaration.routes,
        json(r#"[{"method": "POST", "path": "/v23.0/*/messages"}]"#)
    );
    let nine: Vec<(String, Value)> = (0..9)
        .map(|index| (format!("token-{index}"), stored("Token", &[])))
        .collect();
    assert!(canonical_stored_input_declarations(&nine, &hosts(&["api.example.com"])).is_err());
}

fn two_hosts() -> Vec<String> {
    hosts(&["api.example.com", "other.example.com"])
}

fn token() -> Value {
    stored(
        "Token",
        &[
            ("header", text("Authorization")),
            ("scheme", text("Bearer")),
        ],
    )
}

fn token_with(key: &str, value: Value) -> Value {
    let mut changed = token();
    common::set(&mut changed, key, value);
    changed
}

fn proof(signs: &str, host: &str) -> Value {
    stored(
        "Secret",
        &[
            ("query", text("proof")),
            ("hmac", text(signs)),
            ("host", text(host)),
        ],
    )
}

fn refused(entries: Vec<(&str, Value)>) -> bool {
    canonical_stored_input_declarations(&declarations(entries), &two_hosts()).is_err()
}

#[test]
fn a_proof_signs_exactly_one_plain_input_of_its_own_host() {
    let admitted = canonical_stored_input_declarations(
        &declarations(vec![
            ("token", token()),
            ("secret", proof("token", "api.example.com")),
        ]),
        &two_hosts(),
    )
    .expect("admitted");
    let proofs: Vec<_> = admitted
        .iter()
        .map(|item| (item.id.as_str(), item.hmac.as_deref()))
        .collect();
    assert_eq!(proofs, [("secret", Some("token")), ("token", None)]);
    assert!(refused(vec![
        ("token", token()),
        ("secret", proof("missing", "api.example.com"))
    ]));
    assert!(refused(vec![
        ("token", token()),
        ("secret", proof("secret", "api.example.com"))
    ]));
    assert!(refused(vec![
        ("token", token_with("hmac", text("secret"))),
        ("secret", proof("token", "api.example.com"))
    ]));
    assert!(refused(vec![
        ("token", token()),
        ("secret", proof("token", "other.example.com"))
    ]));
}

#[test]
fn each_placement_has_one_allowed_host_and_one_field_team_does_not_own() {
    let mut without_header = token();
    let Value::Object(members) = &mut without_header else {
        panic!("object")
    };
    members.retain(|(name, _)| name != "header");
    assert!(refused(vec![("token", without_header)]));
    assert!(refused(vec![(
        "token",
        token_with("host", text("collector.example.org"))
    )]));
    assert!(refused(vec![("token", token_with("query", text("key")))]));
    assert!(refused(vec![(
        "token",
        stored(
            "Token",
            &[("query", text("key")), ("scheme", text("Bearer"))]
        )
    )]));
    for header in ["Content-Length", "bad header"] {
        assert!(refused(vec![(
            "token",
            stored("Token", &[("header", text(header))])
        )]));
    }
    assert!(refused(vec![(
        "token",
        stored("Token", &[("query", text("a&b"))])
    )]));
    assert!(refused(vec![
        ("token", token()),
        (
            "other",
            stored("Other", &[("header", text("authorization"))])
        )
    ]));
}

#[test]
fn a_declaration_holds_only_its_closed_reviewed_fields() {
    let credential_route = json(r#"[{"method": "GET", "path": "/oauth/token"}]"#);
    for (key, value) in [
        ("routes", credential_route),
        ("kind", text("text")),
        ("help_url", text("http://example.com/keys")),
        ("label", text("Bearer abcdefghijklmnop")),
        ("value", text("secret")),
    ] {
        assert!(refused(vec![("token", token_with(key, value))]), "{key}");
    }
}

#[test]
fn an_integration_bearer_owns_authorization_on_its_provider_hosts() {
    let refused = canonical_manifest_contract(
        &json(r#"["api.cloudflare.com"]"#),
        &declarations(vec![("cloudflare", json(r#"["dns.read"]"#))]),
        &declarations(vec![(
            "token",
            stored(
                "Token",
                &[
                    ("header", text("Authorization")),
                    ("host", text("api.cloudflare.com")),
                ],
            ),
        )]),
    );
    assert!(refused.is_err());
}

#[test]
fn an_automatic_update_keeps_hosts_within_and_every_retained_placement() {
    let contract_with = |placement: &[(&str, Value)]| {
        let mut fields = vec![("header", text("Authorization"))];
        fields.extend(placement.iter().cloned());
        canonical_manifest_contract(
            &json(r#"["api.example.com", "other.example.com"]"#),
            &[],
            &declarations(vec![("token", stored("Token", &fields))]),
        )
        .expect("admitted")
    };
    let previous = contract_with(&[]);
    assert!(automatic_update_preserves_egress(
        &previous,
        &contract_with(&[])
    ));
    for changed in [
        vec![("host", text("other.example.com"))],
        vec![("header", text("X-Token"))],
        vec![("scheme", text("Bearer"))],
        vec![(
            "routes",
            json(r#"[{"method": "PUT", "path": "/v23.0/*/messages"}]"#),
        )],
    ] {
        assert!(!automatic_update_preserves_egress(
            &previous,
            &contract_with(&changed)
        ));
    }
    let scoped = |hosts: &str, scopes: &str| {
        canonical_manifest_contract(
            &json(hosts),
            &declarations(vec![("cloudflare", json(scopes))]),
            &[],
        )
        .expect("admitted")
    };
    let narrow = scoped(r#"["api.cloudflare.com"]"#, r#"["zone.read"]"#);
    assert!(!automatic_update_preserves_egress(
        &narrow,
        &scoped(
            r#"["api.cloudflare.com", "api.openai.com"]"#,
            r#"["zone.read"]"#
        )
    ));
    assert!(automatic_update_preserves_egress(
        &narrow,
        &scoped(r#"["api.cloudflare.com"]"#, r#"["zone.read", "dns.read"]"#)
    ));
}

fn stored_contract(ids: &[&str]) -> ManifestContract {
    let entries: Vec<(String, Value)> = ids
        .iter()
        .enumerate()
        .map(|(index, id)| {
            (
                (*id).to_owned(),
                stored("Key", &[("header", text(&format!("X-Key-{index}")))]),
            )
        })
        .collect();
    let stored_inputs = canonical_stored_input_declarations(&entries, &hosts(&["api.example.com"]))
        .expect("admitted");
    ManifestContract {
        stored_inputs,
        ..empty()
    }
}

#[test]
fn a_stored_input_needs_the_password_request_and_a_sorted_unique_declared_list() {
    let declared = stored_contract(&[
        "key-1", "key-2", "key-3", "key-4", "key-5", "key-6", "key-7", "key-8",
    ]);
    let machine = |slots: Value, requests: Value| {
        let mut action = common::action("send-message", "mutating");
        common::set(&mut action, "stored_inputs", slots);
        common::set(&mut action, "human_requests", requests);
        contract(vec![action], &["Key"])
    };
    let password = || json(r#"["input:password"]"#);
    let admitted = admit(
        &machine(json(r#"["key-1", "key-2"]"#), password()),
        &declared,
    )
    .expect("admitted");
    assert!(team_protocol::python::value::py_eq(
        &admitted,
        &machine(json(r#"["key-1", "key-2"]"#), password())
    ));
    assert!(admit(&machine(json(r#"["key-1"]"#), json("[]")), &declared).is_err());
    for slots in [
        r#"["key-2", "key-1"]"#,
        r#"["key-1", "key-1"]"#,
        r#"["key-1", "undeclared-key"]"#,
        r#"["key-1", 2]"#,
    ] {
        assert!(
            admit(&machine(json(slots), password()), &declared).is_err(),
            "{slots}"
        );
    }
}

#[test]
fn an_action_declaring_a_proof_also_declares_the_input_it_signs() {
    let entries = declarations(vec![
        (
            "token",
            stored(
                "Token",
                &[
                    ("header", text("Authorization")),
                    ("scheme", text("Bearer")),
                ],
            ),
        ),
        (
            "secret",
            stored(
                "Secret",
                &[("query", text("appsecret_proof")), ("hmac", text("token"))],
            ),
        ),
    ]);
    let declared = ManifestContract {
        stored_inputs: canonical_stored_input_declarations(&entries, &hosts(&["api.example.com"]))
            .expect("admitted"),
        ..empty()
    };
    let machine = |slots: &str| {
        let mut action = common::action("list-items", "read_only");
        common::set(&mut action, "stored_inputs", json(slots));
        common::set(&mut action, "human_requests", json(r#"["input:password"]"#));
        contract(vec![action], &["Token", "Secret"])
    };
    assert!(admit(&machine(r#"["secret", "token"]"#), &declared).is_ok());
    assert!(admit(&machine(r#"["token"]"#), &declared).is_ok());
    assert!(admit(&machine(r#"["secret"]"#), &declared).is_err());
}

#[test]
fn the_contract_uses_exactly_its_declared_integrations_and_canonicalizes() {
    let declared = ManifestContract {
        integrations: canonical_integration_declarations(&declarations(vec![(
            "cloudflare",
            json(r#"["zone.read"]"#),
        )]))
        .expect("reviewed"),
        ..empty()
    };
    let mut uses = common::action("b-run", "read_only");
    common::set(&mut uses, "integrations", json(r#"["cloudflare"]"#));
    let machine = contract(
        vec![uses.clone(), common::action("a-run", "read_only")],
        &[],
    );
    let canonical = admit(&machine, &declared).expect("admitted");
    let Some(Value::Array(actions)) = canonical.get("actions") else {
        panic!("actions")
    };
    assert_eq!(
        actions
            .iter()
            .map(|action| action.get("id").cloned())
            .collect::<Vec<_>>(),
        [Some(text("a-run")), Some(text("b-run"))]
    );
    assert!(
        admit(
            &contract(vec![common::action("a-run", "read_only")], &[]),
            &declared
        )
        .is_err()
    );
    let mut foreign = common::action("run", "read_only");
    common::set(&mut foreign, "integrations", json(r#"["github"]"#));
    assert!(admit(&contract(vec![foreign], &[]), &declared).is_err());
    assert!(admit(&contract(vec![uses.clone(), uses], &[]), &declared).is_err());
}

#[test]
fn every_action_is_closed_displayed_and_singly_authorized() {
    let run = || common::action("run", "read_only");
    let changed = |key: &str, value: Value| {
        let mut action = run();
        common::set(&mut action, key, value);
        action
    };
    for action in [
        changed(
            "description",
            text("An Action description nobody cataloged."),
        ),
        changed("secret", text("x")),
        changed("human_requests", json(r#"["approval", "auth:totp"]"#)),
        changed("input_schema", json(r#"{"type": "object"}"#)),
    ] {
        assert!(admit(&contract(vec![action], &[]), &empty()).is_err());
    }
}

#[test]
fn the_contract_is_one_closed_strict_document_with_its_summary_cataloged() {
    let mut extra = contract(vec![common::action("run", "read_only")], &[]);
    common::set(&mut extra, "extra", Value::Null);
    assert!(admit(&extra, &empty()).is_err());
    let plain = contract(vec![common::action("run", "read_only")], &[]);
    assert!(
        canonical_machine_contract(
            &plain,
            &empty(),
            "Another summary.",
            common::ASSISTANT_DESCRIPTION
        )
        .is_err()
    );
    for raw in [&b""[..], br#"{"version": 1, "version": 1}"#] {
        assert!(
            parse_machine_contract(
                raw,
                &empty(),
                common::SUMMARY,
                common::ASSISTANT_DESCRIPTION
            )
            .is_err()
        );
    }
}
