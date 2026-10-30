use std::os::unix::fs::PermissionsExt;

use strict_json::Value;
use team_protocol::python::json::{CANONICAL, dumps};
use team_protocol::python::value::{int, object_of, text};
use team_protocol::supervisor::{JWT_HEADER, ROUTINE_JWT_HEADER};

use super::assertion::encode_base64url;
use super::fixture::{Keys, LEASE, NOW, SUPERVISOR_ID, Signer, no_body, routine, session, with};
use super::{
    Absence, Evidence, MAX_ASSERTION_BYTES, Refusal, ReplayGuard, RequestBinding, Verifier,
    approval, assurance, one_assertion,
};

const RUN: &str = "/v1/teams/alpha/routines/runs/0123456789abcdef0123456789abcdef/segment";

fn request(method: &str, path: &str) -> RequestBinding {
    RequestBinding {
        method: method.to_owned(),
        path: path.to_owned(),
        body: no_body(),
        model: None,
        decision: None,
        assurance: None,
        authorities: &["session"],
    }
}

fn teams() -> RequestBinding {
    request("GET", "/v1/teams")
}

fn bearer(assertion: &str) -> Vec<u8> {
    format!("Bearer {assertion}").into_bytes()
}

fn signed(keys: &Keys, claims: Vec<(String, Value)>) -> Vec<u8> {
    bearer(&keys.supervisor.supervisor(&Value::Object(claims)))
}

/// A fresh session assertion for `GET /v1/teams` by `signer`.
fn listing(signer: &Signer) -> Vec<u8> {
    bearer(&signer.supervisor(&Value::Object(session("GET", "/v1/teams", no_body()))))
}

fn verify(
    verifier: &Verifier,
    header: &[u8],
    binding: &RequestBinding,
) -> Result<Evidence, Refusal> {
    verifier.verify_supervisor(&[header], binding, NOW)
}

fn segment(value: &Value) -> String {
    encode_base64url(dumps(value, CANONICAL).as_bytes())
}

/// `head.body` signed by the published Supervisor key, whatever the segments hold.
fn raw_signed(head: &str, body: &str) -> Vec<u8> {
    let signing = format!("{head}.{body}");
    let pair = ring::signature::Ed25519KeyPair::from_seed_unchecked(&[1; 32]).expect("key");
    bearer(&format!(
        "{signing}.{}",
        encode_base64url(pair.sign(signing.as_bytes()).as_ref())
    ))
}

#[test]
fn supervisor_header_single_ascii_bounded() {
    let valid = b"Bearer a.b.c".to_vec();
    assert_eq!(one_assertion(&[&valid]), Ok("a.b.c"));
    let long = bearer(&"a".repeat(MAX_ASSERTION_BYTES));
    assert!(one_assertion(&[&long]).is_ok());
    let too_long = bearer(&"a".repeat(MAX_ASSERTION_BYTES + 1));
    for refused in [
        vec![],
        vec![&valid[..], &valid[..]],
        vec![&b"bearer a.b.c"[..]],
        vec![&b"Bearer "[..]],
        vec![&b"Bearer a.b c"[..]],
        vec![&b"Bearer a.b\tc"[..]],
        vec![&b"Bearer a.b\x1fc"[..]],
        vec!["Bearer a.\u{e9}.c".as_bytes()],
        vec![&too_long[..]],
    ] {
        assert_eq!(one_assertion(&refused), Err(Refusal::Denied));
    }
}

#[test]
fn jwt_segments_canonical_json() {
    let keys = Keys::new();
    let verifier = keys.verifier(16);
    let header = object_of(
        JWT_HEADER
            .iter()
            .map(|(name, value)| (*name, text(value)))
            .collect(),
    );
    let head = segment(&header);
    let body = segment(&Value::Object(session("GET", "/v1/teams", no_body())));
    for refused_head in [
        encode_base64url(br#"{"alg": "EdDSA", "kid": "local-supervisor-v1", "typ": "JWT"}"#),
        encode_base64url(br#"{"typ":"JWT","alg":"EdDSA","kid":"local-supervisor-v1"}"#),
        encode_base64url(
            br#"{"alg":"EdDSA","alg":"EdDSA","kid":"local-supervisor-v1","typ":"JWT"}"#,
        ),
        format!("{head}="),
    ] {
        assert_eq!(
            verify(&verifier, &raw_signed(&refused_head, &body), &teams()),
            Err(Refusal::Denied)
        );
    }
    let two_segments = bearer(&format!("{head}.{body}"));
    assert_eq!(
        verify(&verifier, &two_segments, &teams()),
        Err(Refusal::Denied)
    );
    assert!(verify(&verifier, &raw_signed(&head, &body), &teams()).is_ok());
}

#[test]
fn jwt_header_exact() {
    let keys = Keys::new();
    let verifier = keys.verifier(16);
    let claims = Value::Object(session("GET", "/v1/teams", no_body()));
    for header in [
        [
            ("alg", "EdDSA"),
            ("kid", "local-supervisor-v2"),
            ("typ", "JWT"),
        ],
        [
            ("alg", "HS256"),
            ("kid", "local-supervisor-v1"),
            ("typ", "JWT"),
        ],
        [
            ("alg", "EdDSA"),
            ("kid", "local-supervisor-v1"),
            ("typ", "jwt"),
        ],
        ROUTINE_JWT_HEADER,
    ] {
        let assertion = bearer(&keys.supervisor.sign(&header, &claims));
        assert_eq!(
            verify(&verifier, &assertion, &teams()),
            Err(Refusal::Denied)
        );
    }
}

#[test]
fn ed25519_verify() {
    let keys = Keys::new();
    let verifier = keys.verifier(16);
    assert_eq!(
        verify(&verifier, &listing(&Signer::new(9)), &teams()),
        Err(Refusal::Denied)
    );
    let genuine =
        keys.supervisor
            .supervisor(&Value::Object(session("GET", "/v1/teams", no_body())));
    let other =
        keys.supervisor
            .supervisor(&Value::Object(session("GET", "/v1/assistants", no_body())));
    let parts: Vec<&str> = genuine.split('.').collect();
    let swapped = bearer(&format!(
        "{}.{}.{}",
        parts[0],
        other.split('.').nth(1).expect("claims"),
        parts[2]
    ));
    let assistants = request("GET", "/v1/assistants");
    assert_eq!(
        verify(&verifier, &swapped, &assistants),
        Err(Refusal::Denied)
    );
    let evidence = verify(&verifier, &bearer(&genuine), &teams()).expect("verified");
    assert_eq!(evidence.supervisor_id, SUPERVISOR_ID);
    assert_eq!(evidence.key_sha256, keys.supervisor.public().fingerprint());
    assert_eq!(evidence.authority, "session");
    assert_eq!(evidence.assurance, None);
}

/// A chat request bound to a JSON body, a model and a decision credential, and a human assurance.
struct Bound {
    body: Value,
    model: Value,
    decision: Value,
    assurance: Value,
}

impl Bound {
    fn new() -> Self {
        let digest = "2".repeat(64);
        Self {
            body: object_of(vec![
                ("kind", text("json")),
                ("length", int(2)),
                ("sha256", text(&digest)),
            ]),
            model: object_of(vec![
                ("provider", text("openai")),
                ("key_sha256", text(&digest)),
            ]),
            decision: object_of(vec![
                ("provider", text("typesafe")),
                ("key_sha256", text(&digest)),
            ]),
            assurance: object_of(vec![
                ("kind", text("auth:totp")),
                ("challenge_id", text(&"3".repeat(32))),
            ]),
        }
    }

    fn request(&self) -> RequestBinding {
        RequestBinding {
            method: "POST".to_owned(),
            path: "/v1/teams/alpha/chat".to_owned(),
            body: self.body.clone(),
            model: Some(self.model.clone()),
            decision: Some(self.decision.clone()),
            assurance: Some(self.assurance.clone()),
            authorities: &["session"],
        }
    }

    fn claims(&self) -> Vec<(String, Value)> {
        let claims = session("POST", "/v1/teams/alpha/chat", self.body.clone());
        let claims = with(claims, "model", self.model.clone());
        let claims = with(claims, "decision", self.decision.clone());
        with(claims, "assurance", self.assurance.clone())
    }

    fn without(&self, name: &str) -> Vec<(String, Value)> {
        let mut claims = self.claims();
        claims.retain(|(key, _)| key != name);
        claims
    }
}

#[test]
fn claims_bound_to_method_path_body_model_decision_assurance() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let bound = Bound::new();
    let evidence =
        verify(&verifier, &signed(&keys, bound.claims()), &bound.request()).expect("bound");
    assert_eq!(evidence.assurance.as_deref(), Some("auth:totp"));
    let digest = text(&"4".repeat(64));
    let other = |provider: &str| {
        object_of(vec![
            ("provider", text(provider)),
            ("key_sha256", digest.clone()),
        ])
    };
    let password = object_of(vec![
        ("kind", text("auth:password")),
        ("challenge_id", text(&"3".repeat(32))),
    ]);
    for claims in [
        with(bound.claims(), "method", text("PUT")),
        with(bound.claims(), "path", text("/v1/teams/beta/chat")),
        with(bound.claims(), "body", no_body()),
        with(bound.claims(), "model", other("openai")),
        with(bound.claims(), "decision", other("typesafe")),
        with(bound.claims(), "assurance", password),
        with(bound.claims(), "authority", text("host-reset")),
        bound.without("model"),
        bound.without("decision"),
        bound.without("assurance"),
    ] {
        assert_eq!(
            verify(&verifier, &signed(&keys, claims), &bound.request()),
            Err(Refusal::Denied)
        );
    }
}

#[test]
fn host_reset_binds_only_where_admitted() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let host_reset = || {
        signed(
            &keys,
            with(
                session("DELETE", "/v1/space", no_body()),
                "authority",
                text("host-reset"),
            ),
        )
    };
    let mut space_reset = request("DELETE", "/v1/space");
    assert_eq!(
        verify(&verifier, &host_reset(), &space_reset),
        Err(Refusal::Denied)
    );
    space_reset.authorities = &["session", "host-reset"];
    assert_eq!(
        verify(&verifier, &host_reset(), &space_reset).map(|evidence| evidence.authority),
        Ok("host-reset".to_owned())
    );
}

#[test]
fn iat_skew_5s_exp_window_1_to_15() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let at = |issued: i64, expires: i64| {
        let claims = with(session("GET", "/v1/teams", no_body()), "iat", int(issued));
        signed(&keys, with(claims, "exp", int(expires)))
    };
    for (issued, expires) in [(NOW + 5, NOW + 10), (NOW - 15, NOW), (NOW - 1, NOW + 14)] {
        assert!(verify(&verifier, &at(issued, expires), &teams()).is_ok());
    }
    for (issued, expires) in [
        (NOW + 6, NOW + 10),
        (NOW - 16, NOW - 1),
        (NOW, NOW + 16),
        (NOW, NOW),
        (NOW, NOW - 1),
    ] {
        assert_eq!(
            verify(&verifier, &at(issued, expires), &teams()),
            Err(Refusal::Denied),
            "{issued} {expires}"
        );
    }
}

#[test]
fn jti_replay_refused() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let assertion = signed(&keys, session("GET", "/v1/teams", no_body()));
    assert!(verify(&verifier, &assertion, &teams()).is_ok());
    assert_eq!(
        verify(&verifier, &assertion, &teams()),
        Err(Refusal::Denied)
    );
    // The guard is shared: a Routine assertion reusing a consumed id is a replay too.
    let consumed = text(&"9".repeat(32));
    let first = signed(
        &keys,
        with(
            session("GET", "/v1/teams", no_body()),
            "jti",
            consumed.clone(),
        ),
    );
    assert!(verify(&verifier, &first, &teams()).is_ok());
    let replayed = run_assertion(
        &keys.routine,
        with(routine("POST", RUN, no_body()), "jti", consumed),
    );
    assert_eq!(
        verifier.verify_routine(&[&replayed], "POST", RUN, no_body(), NOW),
        Err(Refusal::Denied)
    );
}

#[test]
fn a_refused_assertion_does_not_consume_its_nonce() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let assertion = signed(&keys, session("GET", "/v1/teams", no_body()));
    let elsewhere = request("GET", "/v1/assistants");
    assert_eq!(
        verify(&verifier, &assertion, &elsewhere),
        Err(Refusal::Denied)
    );
    let mut tampered = assertion.clone();
    let last = tampered.len() - 2;
    tampered[last] = if tampered[last] == b'A' { b'B' } else { b'A' };
    assert_eq!(verify(&verifier, &tampered, &teams()), Err(Refusal::Denied));
    assert!(verify(&verifier, &assertion, &teams()).is_ok());
}

#[test]
fn replay_cache_saturation_answers_unavailable() {
    let guard = ReplayGuard::with_capacity(2);
    assert_eq!(guard.consume("a", NOW + 5, NOW), Ok(()));
    assert_eq!(guard.consume("b", NOW + 10, NOW), Ok(()));
    assert_eq!(guard.consume("c", NOW + 10, NOW), Err(Refusal::Unavailable));
    assert_eq!(guard.consume("a", NOW + 5, NOW), Err(Refusal::Denied));
    // Once "a" expired, its room is free again and it is no longer remembered.
    assert_eq!(guard.consume("c", NOW + 10, NOW + 6), Ok(()));
    assert_eq!(
        guard.consume("a", NOW + 20, NOW + 6),
        Err(Refusal::Unavailable)
    );
    let keys = Keys::new();
    let verifier = keys.verifier(1);
    assert!(verify(&verifier, &listing(&keys.supervisor), &teams()).is_ok());
    assert_eq!(
        verify(&verifier, &listing(&keys.supervisor), &teams()),
        Err(Refusal::Unavailable)
    );
}

#[test]
fn tofu_pin_then_pin_only() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    assert!(!keys.files.pin.exists());
    assert!(verify(&verifier, &listing(&keys.supervisor), &teams()).is_ok());
    let pin = std::fs::metadata(&keys.files.pin).expect("pinned");
    assert_eq!(pin.permissions().mode() & 0o7777, 0o600);
    assert_eq!(
        std::fs::read(&keys.files.pin).expect("pin"),
        keys.supervisor.public().to_pem()
    );
    // Admin now publishes another key: only the pin verifies.
    let replacement = Signer::new(7);
    Keys::publish(&keys.files.supervisor, &replacement);
    assert_eq!(
        verify(&verifier, &listing(&replacement), &teams()),
        Err(Refusal::Denied)
    );
    assert!(verify(&verifier, &listing(&keys.supervisor), &teams()).is_ok());
    // A pin outside its custody is unavailable, never replaced.
    std::fs::set_permissions(&keys.files.pin, std::fs::Permissions::from_mode(0o644))
        .expect("mode");
    assert_eq!(
        verify(&verifier, &listing(&keys.supervisor), &teams()),
        Err(Refusal::Unavailable)
    );
}

#[test]
fn published_key_custody_is_exact() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    std::fs::set_permissions(
        &keys.files.supervisor,
        std::fs::Permissions::from_mode(0o444),
    )
    .expect("mode");
    assert_eq!(
        verify(&verifier, &listing(&keys.supervisor), &teams()),
        Err(Refusal::Unavailable)
    );
    assert!(!keys.files.pin.exists());
    Keys::publish(&keys.files.supervisor, &keys.supervisor);
    let mut other_group = keys.files.clone();
    other_group.key_group = Some(keys.files.key_group.expect("group") + 1);
    let verifier = Verifier::new(other_group, ReplayGuard::default());
    assert_eq!(
        verify(&verifier, &listing(&keys.supervisor), &teams()),
        Err(Refusal::Unavailable)
    );
    std::fs::remove_file(&keys.files.supervisor).expect("removed");
    std::fs::write(
        &keys.files.supervisor,
        b"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VuAyEA\n-----END PUBLIC KEY-----\n",
    )
    .expect("written");
    std::fs::set_permissions(
        &keys.files.supervisor,
        std::fs::Permissions::from_mode(0o440),
    )
    .expect("mode");
    let verifier = keys.verifier(64);
    assert_eq!(
        verify(&verifier, &listing(&keys.supervisor), &teams()),
        Err(Refusal::Unavailable)
    );
    let mut no_group = keys.files.clone();
    no_group.key_group = None;
    let verifier = Verifier::new(no_group, ReplayGuard::default());
    assert_eq!(
        verifier.routine_key_fingerprint(),
        Err(Refusal::Unavailable)
    );
}

fn rotation(signer: &Signer) -> Value {
    object_of(vec![("public_key", text(&signer.public_base64url()))])
}

#[test]
fn a_rotation_needs_a_pin_and_a_canonical_key() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let first = keys.supervisor.public().fingerprint();
    let next = Signer::new(5);
    assert_eq!(
        verifier.rotate(&rotation(&next), &first),
        Err(Refusal::Unavailable)
    );
    assert!(verify(&verifier, &listing(&keys.supervisor), &teams()).is_ok());
    for invalid in [
        object_of(vec![("public_key", text("short"))]),
        object_of(vec![
            ("public_key", text(&next.public_base64url())),
            ("extra", int(1)),
        ]),
        object_of(vec![(
            "public_key",
            text(&format!("{}=", next.public_base64url())),
        )]),
    ] {
        assert_eq!(verifier.rotate(&invalid, &first), Err(Refusal::Denied));
    }
}

#[test]
fn rotation_only_under_current_pin() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let first = keys.supervisor.public().fingerprint();
    let next = Signer::new(5);
    assert!(verify(&verifier, &listing(&keys.supervisor), &teams()).is_ok());
    let expected = object_of(vec![
        ("rotated", Value::Bool(true)),
        ("key_sha256", text(&next.public().fingerprint())),
    ]);
    assert_eq!(
        verifier.rotate(&rotation(&next), &first),
        Ok(expected.clone())
    );
    // A retry, signed by either key, succeeds unchanged.
    assert_eq!(
        verifier.rotate(&rotation(&next), &first),
        Ok(expected.clone())
    );
    assert_eq!(
        verifier.rotate(&rotation(&next), &next.public().fingerprint()),
        Ok(expected)
    );
    // A second rotation signed by the key that is no longer pinned is refused.
    assert_eq!(
        verifier.rotate(&rotation(&Signer::new(6)), &first),
        Err(Refusal::Denied)
    );
    assert_eq!(
        std::fs::read(&keys.files.pin).expect("pin"),
        next.public().to_pem()
    );
    // From the switch on, the earlier key's assertions are refused.
    assert_eq!(
        verify(&verifier, &listing(&keys.supervisor), &teams()),
        Err(Refusal::Denied)
    );
    assert!(verify(&verifier, &listing(&next), &teams()).is_ok());
}

#[test]
fn bootstrap_reset_requires_key_absence_proof() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    assert_eq!(
        verifier.require_supervisor_absent(),
        Err(Absence::Established)
    );
    std::fs::remove_file(&keys.files.supervisor).expect("removed");
    assert_eq!(verifier.require_supervisor_absent(), Ok(()));
    std::fs::write(&keys.files.supervisor, b"not a key").expect("written");
    std::fs::set_permissions(
        &keys.files.supervisor,
        std::fs::Permissions::from_mode(0o440),
    )
    .expect("mode");
    assert_eq!(
        verifier.require_supervisor_absent(),
        Err(Absence::Unavailable)
    );
    std::fs::remove_file(&keys.files.supervisor).expect("removed");
    let directory = keys.files.supervisor.parent().expect("directory");
    std::fs::set_permissions(directory, std::fs::Permissions::from_mode(0o770)).expect("mode");
    assert_eq!(
        verifier.require_supervisor_absent(),
        Err(Absence::Unavailable)
    );
    std::fs::remove_dir_all(directory).expect("removed");
    assert_eq!(
        verifier.require_supervisor_absent(),
        Err(Absence::Unavailable)
    );
    // Forgetting the pin succeeds whether or not one exists, and removes it durably.
    assert_eq!(verifier.forget_pin(), Ok(()));
    std::fs::create_dir_all(keys.files.pin.parent().expect("parent")).expect("created");
    std::fs::write(&keys.files.pin, keys.supervisor.public().to_pem()).expect("written");
    assert_eq!(verifier.forget_pin(), Ok(()));
    assert!(!keys.files.pin.exists());
}

fn run_assertion(signer: &Signer, claims: Vec<(String, Value)>) -> Vec<u8> {
    bearer(&signer.routine(&Value::Object(claims)))
}

#[test]
fn routine_assertion_audience_and_lease_fingerprint() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let header = run_assertion(&keys.routine, routine("POST", RUN, no_body()));
    let evidence = verifier
        .verify_routine(&[&header], "POST", RUN, no_body(), NOW)
        .expect("verified");
    assert_eq!(
        evidence.key_fingerprint,
        keys.routine.public().fingerprint()
    );
    assert_eq!(evidence.lease_sha256, LEASE);
    assert_eq!(
        verifier.routine_key_fingerprint(),
        Ok(keys.routine.public().fingerprint())
    );
    let model = object_of(vec![
        ("provider", text("openai")),
        ("key_sha256", text(&"2".repeat(64))),
    ]);
    let edited = |name: &str, value: Value| {
        run_assertion(
            &keys.routine,
            with(routine("POST", RUN, no_body()), name, value),
        )
    };
    // A Supervisor audience, a session authority, a human binding, another key, or another run never runs it.
    for refused in [
        edited("aud", text("team-local")),
        edited("authority", text("session")),
        edited("model", model),
        bearer(
            &keys
                .routine
                .supervisor(&Value::Object(routine("POST", RUN, no_body()))),
        ),
        run_assertion(&keys.supervisor, routine("POST", RUN, no_body())),
        run_assertion(
            &keys.routine,
            routine("POST", "/v1/teams/beta/routines/runs/x/segment", no_body()),
        ),
    ] {
        assert_eq!(
            verifier.verify_routine(&[&refused], "POST", RUN, no_body(), NOW),
            Err(Refusal::Denied)
        );
    }
}

#[test]
fn a_new_routine_key_fences_every_older_lease() {
    let keys = Keys::new();
    let verifier = keys.verifier(64);
    let before = verifier.routine_key_fingerprint();
    Keys::publish(&keys.files.routine, &Signer::new(8));
    assert_ne!(verifier.routine_key_fingerprint(), before);
    let header = run_assertion(&keys.routine, routine("POST", RUN, no_body()));
    assert_eq!(
        verifier.verify_routine(&[&header], "POST", RUN, no_body(), NOW),
        Err(Refusal::Denied)
    );
    std::fs::remove_file(&keys.files.routine).expect("removed");
    let header = run_assertion(&keys.routine, routine("POST", RUN, no_body()));
    assert_eq!(
        verifier.verify_routine(&[&header], "POST", RUN, no_body(), NOW),
        Err(Refusal::Unavailable)
    );
}

#[test]
fn only_an_approval_of_an_authentication_challenge_binds_assurance() {
    let id = "3".repeat(32);
    let approve = object_of(vec![
        ("challenge_id", text(&id)),
        ("decision", text("submit")),
        ("value", Value::Bool(true)),
    ]);
    let challenge = approval(&approve).expect("approval");
    assert_eq!(
        assurance(challenge, Some(("auth:passkey", &id))),
        Some(object_of(vec![
            ("kind", text("auth:passkey")),
            ("challenge_id", text(&id))
        ]))
    );
    assert_eq!(assurance(challenge, Some(("approval", &id))), None);
    assert_eq!(
        assurance(challenge, Some(("auth:totp", &"4".repeat(32)))),
        None
    );
    assert_eq!(assurance(challenge, None), None);
    for not_approval in [
        object_of(vec![
            ("challenge_id", text(&id)),
            ("decision", text("deny")),
            ("value", Value::Bool(true)),
        ]),
        object_of(vec![
            ("challenge_id", text(&id)),
            ("decision", text("submit")),
            ("value", int(1)),
        ]),
        object_of(vec![
            ("challenge_id", text(&id)),
            ("decision", text("submit")),
        ]),
    ] {
        assert_eq!(approval(&not_approval), None);
    }
}
