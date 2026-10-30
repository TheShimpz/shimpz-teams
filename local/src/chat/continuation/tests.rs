use assistant_protocol::catalog::{catalog_digest, message_id};
use assistant_protocol::human_request::fingerprint;
use strict_json::Value;
use team_action::challenges::{HumanRequirement, render_copy};
use team_action::human::{ActionTranscript, HumanResponse, validate_request};
use team_assistant::language::LanguagePack;
use team_chat::orchestrator::{ChatContinuation, InvokedAction};
use team_inference::memory::Source;
use team_inference::runtime::{ActionRequest, RuntimeTurn, TurnStatus};
use team_inference::usage::TurnUsage;
use team_integrations::pending::{IntegrationRequirement, RequiredIntegration};
use team_protocol::python::json::canonical;
use team_protocol::python::value::{int, object_of};

use super::{CodecError, MAX_PLAINTEXT_BYTES, decode, encode, pending_payload};
use crate::chat::pending::{Identity, PendingLocalChat, Requirements};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

const IMAGE: &str = "ghcr.io/theshimpz/shimpz-assistant@sha256:1111111111111111111111111111111111111111111111111111111111111111";

fn turn() -> RuntimeTurn {
    RuntimeTurn {
        status: TurnStatus::ActionRequired,
        reply: String::new(),
        actions: vec![ActionRequest {
            interrupt_id: "interrupt-1".to_owned(),
            assistant_id: "shimpz-cloudflare".to_owned(),
            action: "list-zones".to_owned(),
            input: json(r#"{"zone":"example.com"}"#),
        }],
        clarification: None,
        memory: Vec::new(),
        routine: None,
        browser: None,
    }
}

fn pending() -> PendingLocalChat {
    PendingLocalChat {
        continuation: ChatContinuation {
            turn: turn(),
            seen_interrupts: vec!["earlier".to_owned()],
            invoked: vec![InvokedAction {
                assistant_id: "shimpz-cloudflare".to_owned(),
                action: "list-zones".to_owned(),
            }],
            round_index: 1,
            file_actions: 0,
        },
        assistant_ids: vec!["shimpz-cloudflare".to_owned()],
        file_ids: Vec::new(),
        provider: Some("openai".to_owned()),
        identity: Identity::Chat {
            team_name: "Marketing".to_owned(),
            network_id: "n".repeat(64),
            assistants: vec![(
                "shimpz-cloudflare".to_owned(),
                IMAGE.to_owned(),
                "c".repeat(64),
            )],
            files: Vec::new(),
            inference: ("openai".to_owned(), "gpt-6-luna".to_owned()),
        },
        transcripts: Vec::new(),
        requests_used: 0,
        locale: Some("pt".to_owned()),
        usage: Some(TurnUsage {
            started_ms: 1_700_000_000_000,
            models: vec![("openai".to_owned(), "gpt-6-luna".to_owned(), 1331, 36)],
        }),
        paused_batch: None,
        recording: Some("a".repeat(32)),
        model: Some("gpt-6-luna".to_owned()),
        source: Some(Source {
            nonce: "b".repeat(32),
            actor_id: "c".repeat(32),
            incarnation: "f".repeat(64),
        }),
    }
}

fn integrations() -> Requirements {
    Requirements::Integrations(vec![IntegrationRequirement {
        assistant_id: "shimpz-cloudflare".to_owned(),
        assistant_name: "Shimpz Cloudflare".to_owned(),
        action_ids: vec!["list-zones".to_owned()],
        integrations: vec![RequiredIntegration {
            id: "cloudflare".to_owned(),
            provider: "cloudflare".to_owned(),
            scopes: vec!["zone:read".to_owned()],
        }],
    }])
}

fn message(text: &str) -> Value {
    object_of(vec![
        ("id", Value::String(message_id(text))),
        ("msgid", Value::String(text.to_owned())),
        ("max_length", int(80)),
        ("params", Value::Array(Vec::new())),
    ])
}

/// An admitted approval request of the fixture binding and the English copy rendered from its pack.
fn approval() -> (
    team_action::human::HumanRequest,
    team_action::challenges::RequestCopy,
) {
    let (title, description) = ("List zones", "Allow listing the zones.");
    let reference = |text: &str| {
        object_of(vec![
            ("message", Value::String(message_id(text))),
            ("params", Value::Object(Vec::new())),
        ])
    };
    let request = object_of(vec![
        ("kind", Value::String("approval".to_owned())),
        ("ordinal", int(0)),
        ("title", reference(title)),
        ("description", reference(description)),
    ]);
    let Value::Object(mut framed) = request.clone() else {
        unreachable!()
    };
    framed.push((
        "fingerprint".to_owned(),
        Value::String(fingerprint(&request)),
    ));
    let mut messages = vec![message(title), message(description)];
    messages.sort_by_key(|entry| canonical(entry.get("id").expect("id")));
    let messages = Value::Array(messages);
    let admitted = validate_request(
        &Value::Object(framed),
        &["approval".to_owned()],
        &[],
        &messages,
    );
    let request = admitted.expect("request");
    let pack = LanguagePack {
        catalog_digest: catalog_digest(&messages),
        pack_digest: format!("sha256:{}", "a".repeat(64)),
        messages,
        translations: Value::Object(Vec::new()),
        size: 0,
    };
    let copy = render_copy(&request, &pack, "en", None).expect("copy");
    (request, copy)
}

fn human() -> Requirements {
    let (request, copy) = approval();
    let shown =
        r#"{"fields":[{"name":"zone","value":"\"example.com\"","truncated":false}],"omitted":0}"#;
    Requirements::Human(Box::new(HumanRequirement {
        assistant_id: "shimpz-cloudflare".to_owned(),
        assistant_name: "Shimpz Cloudflare".to_owned(),
        action_id: "list-zones".to_owned(),
        action_summary: "List zones".to_owned(),
        interrupt_id: "interrupt-1".to_owned(),
        request,
        assistant_version: "1.0.0".to_owned(),
        copy,
        help_url: None,
        help_text: None,
        purpose: Some("To list your zones.".to_owned()),
        purpose_locale: Some("pt".to_owned()),
        file: None,
        input: Some(json(shown)),
    }))
}

fn human_pending() -> PendingLocalChat {
    let mut paused = pending();
    paused.paused_batch = Some("d".repeat(64));
    paused.transcripts = vec![ActionTranscript {
        interrupt_id: "interrupt-1".to_owned(),
        responses: vec![HumanResponse {
            kind: "input:text".to_owned(),
            ordinal: 0,
            fingerprint: "e".repeat(64),
            value: json("\"zone\""),
        }],
    }];
    paused.requests_used = 1;
    paused
}

#[test]
fn an_integration_pause_round_trips_and_binds_each_action_to_its_release() {
    let (bound, payload) =
        encode(&integrations(), &pending(), MAX_PLAINTEXT_BYTES).expect("encoded");
    assert_eq!(
        bound,
        vec![format!("shimpz-cloudflare/list-zones/{IMAGE}/-")]
    );
    let decoded = decode("integrations", &payload, &bound).expect("decoded");
    assert_eq!(
        (decoded.requirements, decoded.pending),
        (integrations(), pending())
    );
    // Another binding, kind, or schema never opens it.
    assert!(
        decode(
            "integrations",
            &payload,
            &[format!("shimpz-cloudflare/other/{IMAGE}/-")]
        )
        .is_err()
    );
    assert!(decode("human", &payload, &bound).is_err());
}

#[test]
fn a_human_pause_round_trips_and_binds_its_request_fingerprint() {
    let requirements = human();
    let (bound, payload) =
        encode(&requirements, &human_pending(), MAX_PLAINTEXT_BYTES).expect("encoded");
    let Requirements::Human(requirement) = &requirements else {
        unreachable!()
    };
    assert_eq!(
        bound,
        vec![format!(
            "shimpz-cloudflare/list-zones/{IMAGE}/{}",
            requirement.request.fingerprint
        )]
    );
    let decoded = decode("human", &payload, &bound).expect("decoded");
    assert_eq!(decoded.pending, human_pending());
    assert_eq!(
        super::body(&decoded.requirements, &decoded.pending),
        payload
    );
    // A human pause names exactly the batch it holds; an Integration pause holds none.
    assert!(encode(&requirements, &pending(), MAX_PLAINTEXT_BYTES).is_err());
    assert!(encode(&integrations(), &human_pending(), MAX_PLAINTEXT_BYTES).is_err());
}

#[test]
fn a_frozen_routine_run_names_no_provider_model_usage_recording_or_source() {
    let mut run = human_pending();
    run.provider = None;
    run.identity = Identity::Routine {
        network_id: "n".repeat(64),
        assistants: vec![(
            "shimpz-cloudflare".to_owned(),
            IMAGE.to_owned(),
            "c".repeat(64),
        )],
    };
    run.usage = None;
    run.recording = None;
    run.model = None;
    run.source = None;
    let (bound, payload) = encode(&human(), &run, MAX_PLAINTEXT_BYTES).expect("encoded");
    assert_eq!(
        decode("human", &payload, &bound)
            .expect("decoded")
            .pending
            .identity,
        run.identity
    );
    let mut named = run.clone();
    named.model = Some("gpt-6-luna".to_owned());
    assert!(encode(&human(), &named, MAX_PLAINTEXT_BYTES).is_err());
}

fn reencoded(change: impl Fn(&mut Vec<(String, Value)>)) -> Result<super::Decoded, CodecError> {
    let (bound, _) = encode(&integrations(), &pending(), MAX_PLAINTEXT_BYTES).expect("encoded");
    let Value::Object(mut members) = pending_payload(&pending()) else {
        unreachable!()
    };
    change(&mut members);
    let body = object_of(vec![
        ("schema", int(10)),
        ("kind", Value::String("integrations".to_owned())),
        (
            "requirements",
            json(
                r#"[{"assistant_id":"shimpz-cloudflare","assistant_name":"Shimpz Cloudflare","action_ids":["list-zones"],"integrations":[["cloudflare","cloudflare",["zone:read"]]]}]"#,
            ),
        ),
        ("pending", Value::Object(members)),
    ]);
    decode("integrations", &canonical(&body), &bound)
}

fn set(members: &mut [(String, Value)], name: &str, value: &Value) {
    for member in members.iter_mut() {
        if member.0 == name {
            member.1 = value.clone();
        }
    }
}

#[test]
fn every_pending_field_is_admitted_closed() {
    assert!(reencoded(|_| {}).is_ok());
    let refused: [(&str, &str); 12] = [
        ("provider", "\"anthropic\""),
        ("provider", "null"),
        ("model", "\"gpt-unknown\""),
        ("locale", "\"xx\""),
        ("recording", "\"short\""),
        ("requests_used", "17"),
        ("assistant_ids", r#"["b-assistant","a-assistant"]"#),
        ("file_ids", r#"["not-a-file"]"#),
        ("usage", r#"{"started_ms":-1,"models":[]}"#),
        ("usage", r#"{"started_ms":1,"models":{}}"#),
        ("source", r#"{"nonce":"x"}"#),
        (
            "transcripts",
            r#"[{"interrupt_id":"i","responses":[{"kind":"input:password","ordinal":0,"fingerprint":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","value":"secret"}]}]"#,
        ),
    ];
    for (name, value) in refused {
        assert!(
            reencoded(|members| set(members, name, &json(value))).is_err(),
            "{name}: {value}"
        );
    }
    let mut usage = pending();
    usage.usage = Some(TurnUsage {
        started_ms: 7,
        models: Vec::new(),
    });
    let (bound, payload) = encode(&integrations(), &usage, MAX_PLAINTEXT_BYTES).expect("encoded");
    assert_eq!(
        decode("integrations", &payload, &bound)
            .expect("decoded")
            .pending
            .usage,
        usage.usage
    );
}

#[test]
fn a_continuation_beyond_its_bound_is_never_kept() {
    let mut large = pending();
    large.continuation.turn.actions[0].input = object_of(vec![(
        "blob",
        Value::String("x".repeat(MAX_PLAINTEXT_BYTES)),
    )]);
    assert_eq!(
        encode(&integrations(), &large, MAX_PLAINTEXT_BYTES),
        Err(CodecError("continuation exceeds its fixed byte limit"))
    );
}
