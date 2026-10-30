use strict_json::Value;

use super::{
    ActionTranscript, HumanRequest, MAX_REQUESTS_PER_TURN, admit_response, append_response,
    retain_unfinished, validate_request,
};
use crate::journal::{canonical, sha256_hex};

fn vectors() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../protocol/assistant/v1/vectors/human-request.json"
    );
    strict_json::parse(&std::fs::read(path).expect("vectors")).expect("json")
}

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn kinds() -> Vec<String> {
    [
        "approval",
        "auth:password",
        "input:text",
        "input:textarea",
        "input:password",
        "input:select",
        "input:choice",
        "input:choices",
        "input:phone",
        "auth:totp",
        "auth:passkey",
    ]
    .map(str::to_owned)
    .to_vec()
}

/// `request` with its fingerprint.
fn fingerprinted(request: &Value) -> Value {
    let Value::Object(mut members) = request.clone() else {
        panic!("an object")
    };
    members.push((
        "fingerprint".to_owned(),
        Value::String(sha256_hex(&canonical(request))),
    ));
    Value::Object(members)
}

fn messages() -> Value {
    vectors()
        .get("catalog")
        .and_then(|catalog| catalog.get("messages"))
        .expect("messages")
        .clone()
}

fn cases() -> Vec<(String, bool, Value)> {
    let Some(Value::Array(cases)) = vectors().get("request_cases").cloned() else {
        panic!("cases")
    };
    cases
        .into_iter()
        .map(|case| {
            let name = match case.get("name") {
                Some(Value::String(name)) => name.clone(),
                _ => String::new(),
            };
            (
                name,
                case.get("valid") == Some(&Value::Bool(true)),
                case.get("request").expect("request").clone(),
            )
        })
        .collect()
}

fn admitted(kind: &str) -> HumanRequest {
    let (_, _, request) = cases()
        .into_iter()
        .find(|(_, valid, request)| {
            *valid && request.get("kind") == Some(&Value::String(kind.to_owned()))
        })
        .expect("a vector");
    validate_request(
        &fingerprinted(&request),
        &kinds(),
        &["whatsapp-token".to_owned()],
        &messages(),
    )
    .expect("admitted")
}

#[test]
fn team_admission_decides_every_published_request_vector() {
    let declared = ["whatsapp-token".to_owned()];
    for (name, valid, request) in cases() {
        let Value::Object(_) = request else { continue };
        let verdict = validate_request(&fingerprinted(&request), &kinds(), &declared, &messages());
        assert_eq!(verdict.is_ok(), valid, "{name}");
    }
}

#[test]
fn the_fingerprint_covers_the_canonical_references_and_never_a_display_language() {
    let Some(Value::Array(cases)) = vectors()
        .get("fingerprint")
        .and_then(|fingerprint| fingerprint.get("cases"))
        .cloned()
    else {
        panic!("cases")
    };
    for case in cases {
        let request = case.get("request").expect("request");
        assert_eq!(
            Some(&Value::String(sha256_hex(&canonical(request)))),
            case.get("sha256")
        );
    }
}

#[test]
fn request_admission_refuses_capability_and_fingerprint_drift() {
    let request = admitted("approval").payload();
    let Value::Object(members) = &request else {
        panic!("an object")
    };
    let stripped = Value::Object(
        members
            .iter()
            .filter(|(name, _)| name != "fingerprint")
            .cloned()
            .collect(),
    );
    assert!(validate_request(&request, &["input:text".to_owned()], &[], &messages()).is_err());
    let forged = Value::Object(
        members
            .iter()
            .map(|(name, value)| {
                if name == "fingerprint" {
                    (name.clone(), Value::String("0".repeat(64)))
                } else {
                    (name.clone(), value.clone())
                }
            })
            .collect(),
    );
    assert!(validate_request(&forged, &kinds(), &[], &messages()).is_err());
    assert!(validate_request(&stripped, &kinds(), &[], &messages()).is_err());
    assert!(validate_request(&request, &kinds(), &[], &json("[]")).is_err());
    assert!(validate_request(&request, &kinds(), &[], &messages()).is_ok());
}

#[test]
fn approval_and_auth_require_success_and_text_obeys_its_bounds() {
    let approval = admitted("approval");
    assert!(admit_response(&approval, &Value::Bool(true)).is_ok());
    assert!(
        admit_response(&approval, &Value::Bool(false)).is_err()
            && admit_response(&approval, &json(r#""yes""#)).is_err()
    );
    let text = admitted("input:text");
    let maximum = match text.payload().get("max_length") {
        Some(Value::Number(strict_json::Number::Int(maximum))) => {
            usize::try_from(*maximum).expect("bound")
        }
        _ => panic!("a bound"),
    };
    assert!(admit_response(&text, &Value::String("x".repeat(maximum))).is_ok());
    assert!(admit_response(&text, &Value::String("x".repeat(maximum + 1))).is_err());
    assert!(admit_response(&text, &Value::Bool(true)).is_err());
}

#[test]
fn choices_are_closed_to_reviewed_values() {
    let select = admitted("input:choice");
    let Some(Value::Array(options)) = select.payload().get("options").cloned() else {
        panic!("options")
    };
    let first = options[0].get("value").expect("value").clone();
    assert!(admit_response(&select, &first).is_ok());
    assert!(admit_response(&select, &json(r#""not-an-option""#)).is_err());
    let unique = admitted("approval");
    assert!(admit_response(&unique, &Value::Array(Vec::new())).is_err());
}

#[test]
fn a_transcript_requires_the_exact_sequence_and_authorizes_once() {
    let approval = admitted("approval");
    let transcript = ActionTranscript::new("interrupt-1")
        .append(&approval, &Value::Bool(true))
        .expect("appended");
    assert!(transcript.authorized(&["approval".to_owned()]));
    assert!(!transcript.authorized(&["auth:totp".to_owned()]));
    assert!(transcript.append(&approval, &Value::Bool(true)).is_err());
    assert_eq!(
        transcript.payloads()[0].get("value"),
        Some(&Value::Bool(true))
    );
}

#[test]
fn a_stored_input_answer_is_sealed_never_replayed_and_counts_against_the_turn() {
    let password = admitted("input:password");
    if password.stored_input.is_none() {
        return;
    }
    let admission = append_response(
        &[],
        "interrupt-1",
        &password,
        &json(r#""a-secret-value""#),
        0,
    )
    .expect("admitted");
    assert_eq!(admission.transcripts.len(), 0);
    assert_eq!(admission.requests_used, 1);
    let (id, value) = admission.stored_input.expect("sealed");
    assert_eq!(
        (id.as_str(), value.expose()),
        ("whatsapp-token", "a-secret-value")
    );
    assert!(
        ActionTranscript::new("interrupt-1")
            .append(&password, &json(r#""x""#))
            .is_err()
    );
}

#[test]
fn turn_transcripts_are_interrupt_bound_and_globally_bounded() {
    let approval = admitted("approval");
    let first =
        append_response(&[], "interrupt-1", &approval, &Value::Bool(true), 0).expect("admitted");
    let second = append_response(
        &first.transcripts,
        "interrupt-2",
        &approval,
        &Value::Bool(true),
        first.requests_used,
    )
    .expect("admitted");
    assert_eq!((second.transcripts.len(), second.requests_used), (2, 2));
    assert!(
        append_response(
            &[],
            "interrupt-3",
            &approval,
            &Value::Bool(true),
            MAX_REQUESTS_PER_TURN
        )
        .is_err()
    );
    let duplicated = [
        ActionTranscript::new("interrupt-1"),
        ActionTranscript::new("interrupt-1"),
    ];
    assert!(append_response(&duplicated, "interrupt-1", &approval, &Value::Bool(true), 0).is_err());
    let kept = retain_unfinished(&second.transcripts, &["interrupt-1".to_owned()]);
    assert_eq!(
        kept.iter()
            .map(|transcript| transcript.interrupt_id.as_str())
            .collect::<Vec<_>>(),
        ["interrupt-2"]
    );
}
