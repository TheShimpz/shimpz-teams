use hyper::HeaderMap;
use hyper::header::HeaderValue;
use strict_json::Value;
use team_core::secret::Secret;

use super::{
    admissible_api_key, bearer_matches, canonical_path, decision_key, empty_body, file_metadata,
    json_document, json_length, model_credential, upgrade_key,
};
use crate::http::operation::Operation;

fn headers(pairs: &[(&'static str, &[u8])]) -> HeaderMap {
    let mut map = HeaderMap::new();
    for (name, value) in pairs {
        map.append(*name, HeaderValue::from_bytes(value).expect("header value"));
    }
    map
}

fn code(
    result: Result<impl std::fmt::Debug, crate::http::service::Problem>,
) -> (u16, &'static str) {
    let problem = result.expect_err("refused");
    (problem.status, problem.code.expect("code"))
}

#[test]
fn the_bearer_is_one_exact_header_compared_whole() {
    let token = Secret::new("a".repeat(64));
    let exact = format!("Bearer {}", "a".repeat(64));
    assert!(bearer_matches(
        &headers(&[("authorization", exact.as_bytes())]),
        &token
    ));
    for refused in [
        headers(&[]),
        headers(&[
            ("authorization", exact.as_bytes()),
            ("authorization", exact.as_bytes()),
        ]),
        headers(&[(
            "authorization",
            format!("bearer {}", "a".repeat(64)).as_bytes(),
        )]),
        headers(&[(
            "authorization",
            format!("Bearer  {}", "a".repeat(64)).as_bytes(),
        )]),
        headers(&[(
            "authorization",
            format!("Bearer {}", "a".repeat(63)).as_bytes(),
        )]),
        headers(&[(
            "authorization",
            format!("Bearer {}", "b".repeat(64)).as_bytes(),
        )]),
    ] {
        assert!(!bearer_matches(&refused, &token));
    }
}

#[test]
fn only_a_canonical_origin_form_path_routes() {
    let path = |target: &str| canonical_path(&target.parse().expect("uri")).map(str::to_owned);
    assert_eq!(path("/v1/teams/alpha"), Ok("/v1/teams/alpha".to_owned()));
    assert_eq!(path("/"), Ok("/".to_owned()));
    assert_eq!(path("/healthz?"), Ok("/healthz".to_owned()));
    for refused in [
        "/v1//teams",
        "/v1/teams/",
        "/v1/teams?x=1",
        "/v1/te%61ms",
        "http://127.0.0.1/v1/teams",
    ] {
        assert_eq!(code(path(refused)), (400, "invalid-path"), "{refused}");
    }
    assert_eq!(
        code(path(&format!("/{}", "a".repeat(512)))),
        (414, "path-too-long")
    );
    assert!(path(&format!("/{}", "a".repeat(511))).is_ok());
}

fn json(pairs: &[(&'static str, &[u8])]) -> Result<usize, crate::http::service::Problem> {
    json_length(&headers(pairs), 16 * 1024)
}

const TYPED: (&str, &[u8]) = ("content-type", b"Application/JSON ; charset=utf-8");

#[test]
fn a_json_body_is_framed_by_one_length() {
    assert_eq!(json(&[TYPED, ("content-length", b"2")]), Ok(2));
    assert_eq!(code(json(&[TYPED])), (411, "content-length"));
    assert_eq!(
        code(json(&[
            TYPED,
            ("content-length", b"2"),
            ("content-length", b"2")
        ])),
        (411, "content-length")
    );
    assert_eq!(
        code(json(&[TYPED, ("transfer-encoding", b"chunked")])),
        (400, "chunked-request")
    );
    assert_eq!(
        code(json(&[TYPED, ("content-length", b"+2")])),
        (400, "content-length")
    );
}

#[test]
fn a_json_body_is_bounded_and_declared_as_json() {
    for length in [&b"1"[..], b"16385", b"99999999999999999999999"] {
        assert_eq!(
            code(json(&[TYPED, ("content-length", length)])),
            (413, "body-too-large")
        );
    }
    assert_eq!(
        code(json(&[
            ("content-type", b"text/plain"),
            ("content-length", b"2")
        ])),
        (415, "content-type")
    );
    assert_eq!(
        code(json(&[("content-length", b"2")])),
        (415, "content-type")
    );
}

#[test]
fn a_json_body_is_one_strict_object_bound_by_its_digest() {
    assert_eq!(code(json_document(b"[]")), (422, "invalid-body"));
    assert_eq!(
        code(json_document(br#"{"a":1,"a":2}"#)),
        (400, "invalid-json")
    );
    assert_eq!(code(json_document(b"{\"a\":NaN}")), (400, "invalid-json"));
    let (document, binding) = json_document(b"{}").expect("object");
    assert_eq!(document, Value::Object(vec![]));
    assert_eq!(
        binding.get("sha256"),
        Some(&Value::String(
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a".to_owned()
        ))
    );
}

#[test]
fn a_bodyless_operation_admits_no_body() {
    assert!(empty_body(&headers(&[])).is_ok());
    assert!(empty_body(&headers(&[("content-length", b"0")])).is_ok());
    assert_eq!(
        code(empty_body(&headers(&[("content-length", b"1")]))),
        (400, "unexpected-body")
    );
    assert_eq!(
        code(empty_body(&headers(&[("transfer-encoding", b"chunked")]))),
        (400, "unexpected-body")
    );
    assert_eq!(
        code(empty_body(&headers(&[
            ("content-length", b"0"),
            ("content-length", b"0")
        ]))),
        (400, "content-length")
    );
}

#[test]
fn an_upload_names_one_exactly_encoded_admissible_file() {
    let upload = |name: &[u8], media_type: &[u8], length: &[u8]| {
        file_metadata(
            &headers(&[
                ("content-length", length),
                ("content-type", media_type),
                ("x-shimpz-filename", name),
            ]),
            1024,
        )
    };
    let metadata =
        upload(b"r%C3%A9sum%C3%A9%20final.pdf", b"application/pdf", b"10").expect("admitted");
    assert_eq!(metadata.filename, "résumé final.pdf");
    assert_eq!(metadata.length, 10);
    for name in [
        &b""[..],
        b"..",
        b"a%2Fb",
        b"a%5Cb",
        b"%20a",
        b"a%0Ab",
        b"a b",
        b"r%c3%a9",
        b"%FF",
        b"a%7E",
    ] {
        assert_eq!(
            code(upload(name, b"text/plain", b"10")),
            (422, "invalid-file"),
            "{name:?}"
        );
    }
    assert!(upload(b"a~b", b"text/plain", b"10").is_ok());
    assert_eq!(
        code(upload(b"a", b"Text/Plain", b"10")),
        (415, "content-type")
    );
    assert_eq!(code(upload(b"a", b"text", b"10")), (415, "content-type"));
    assert_eq!(
        code(upload(b"a", b"text/plain", b"0")),
        (413, "file-too-large")
    );
    assert_eq!(
        code(upload(b"a", b"text/plain", b"1025")),
        (413, "file-too-large")
    );
}

const KEY: &[u8] = b"sk-test-key-value-long";

#[test]
fn api_key_charset_and_length() {
    assert!(admissible_api_key(&[b'k'; 16]));
    assert!(admissible_api_key(&[b'~'; 8192]));
    for refused in [
        &[b'k'; 15][..],
        &[b'k'; 8193],
        b"sk-test key value",
        "sk-test-\u{e9}-value-long".as_bytes(),
        b"sk-test-value-long\x7f",
    ] {
        assert!(!admissible_api_key(refused));
    }
}

fn model(pairs: &[(&'static str, &[u8])]) -> Result<String, crate::http::service::Problem> {
    model_credential(&headers(pairs)).map(|(provider, _)| provider)
}

#[test]
fn a_model_credential_is_one_known_provider_and_one_admissible_key() {
    assert_eq!(
        model(&[
            ("x-shimpz-model-provider", b"openai"),
            ("x-shimpz-model-api-key", KEY)
        ]),
        Ok("openai".to_owned())
    );
    for refused in [
        model(&[
            ("x-shimpz-model-provider", b"other"),
            ("x-shimpz-model-api-key", KEY),
        ]),
        model(&[("x-shimpz-model-api-key", KEY)]),
        model(&[
            ("x-shimpz-model-provider", b"openai"),
            ("x-shimpz-model-api-key", KEY),
            ("x-shimpz-model-api-key", KEY),
        ]),
        model(&[
            ("x-shimpz-model-provider", b"openai"),
            ("x-shimpz-model-api-key", b"short"),
        ]),
    ] {
        let problem = refused.expect_err("refused");
        assert_eq!(
            (problem.status, problem.code),
            (422, Some("invalid-model-credential"))
        );
        assert!(!problem.message.contains("sk-test"));
    }
}

#[test]
fn a_decision_key_is_admitted_only_on_intent_routing() {
    let decision =
        |operation, pairs: &[(&'static str, &[u8])]| decision_key(&headers(pairs), operation);
    assert_eq!(decision(Operation::Chat, &[]), Ok(None));
    assert!(
        decision(
            Operation::ChatIntentRoute,
            &[("x-shimpz-decision-api-key", KEY)]
        )
        .expect("admitted")
        .is_some()
    );
    for refused in [
        decision(Operation::Chat, &[("x-shimpz-decision-api-key", KEY)]),
        decision(
            Operation::ChatIntentRoute,
            &[("x-shimpz-decision-api-key", b"short")],
        ),
        decision(
            Operation::ChatIntentRoute,
            &[
                ("x-shimpz-decision-api-key", KEY),
                ("x-shimpz-decision-api-key", KEY),
            ],
        ),
    ] {
        assert_eq!(code(refused), (422, "invalid-decision-credential"));
    }
}

#[test]
fn a_view_upgrade_is_one_exact_websocket_upgrade() {
    let upgrade: Vec<(&'static str, &[u8])> = vec![
        ("upgrade", b"WebSocket"),
        ("connection", b"keep-alive, Upgrade"),
        ("sec-websocket-version", b"13"),
        ("sec-websocket-protocol", b"binary"),
        ("sec-websocket-key", b"dGhlIHNhbXBsZSBub25jZQ=="),
    ];
    assert_eq!(
        upgrade_key(&headers(&upgrade)),
        Some("dGhlIHNhbXBsZSBub25jZQ==".to_owned())
    );
    for (index, value) in [
        (0, &b"h2c"[..]),
        (1, b"keep-alive"),
        (2, b"8"),
        (3, b"text"),
        (4, b"short"),
    ] {
        let mut changed = upgrade.clone();
        changed[index].1 = value;
        assert_eq!(upgrade_key(&headers(&changed)), None);
    }
    let mut extended = upgrade.clone();
    extended.push(("sec-websocket-extensions", b"permessage-deflate"));
    assert_eq!(upgrade_key(&headers(&extended)), None);
    let mut repeated = upgrade;
    repeated.push(("sec-websocket-key", b"dGhlIHNhbXBsZSBub25jZQ=="));
    assert_eq!(upgrade_key(&headers(&repeated)), None);
}
