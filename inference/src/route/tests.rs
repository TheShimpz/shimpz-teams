use strict_json::Value;
use team_core::secret::Secret;
use team_core::stop::Stop;

use super::{
    Capability, CapabilityCandidate, CapabilityIntegration, CapabilityPlan, DirectoryCandidate,
    Installable, IntentRoute, IntentRouteRequest, LifecycleContext, LifecycleIntent,
    LifecycleReference, PlanStatus, RouteError, RouteIntent, capability_plan, intent_route,
    parse_intent_route,
};
use crate::brain::{BrainError, BrainOperation, ModelCredential};
use crate::runtime::ConversationEntry;
use crate::testing::ScriptedBrain;
use crate::usage::Unmetered;

const USAGE: &str = r#""usage":{"model_calls":1,"provider_requests":1,"failed_calls":0,"unreported_calls":0,
"input_tokens":1,"output_tokens":1,"cache_read_tokens":0,"cache_write_tokens":0}"#;
const DECISION_KEY: &str = "decision-test-key";

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn answer(fields: &str) -> Value {
    json(&format!("{{{fields},{USAGE}}}"))
}

fn credential(provider: &str) -> ModelCredential {
    ModelCredential {
        provider: provider.to_owned(),
        model: "gpt-6.1-sol".to_owned(),
        api_key: Secret::new("test-key-not-real-0000".to_owned()),
    }
}

fn integration(id: &str) -> CapabilityIntegration {
    CapabilityIntegration {
        id: id.to_owned(),
        provider: id.to_owned(),
    }
}

fn capability_candidates() -> Vec<CapabilityCandidate> {
    vec![
        CapabilityCandidate {
            id: "shimpz-cloudflare".to_owned(),
            name: "Shimpz Cloudflare".to_owned(),
            summary: "Manage reviewed DNS records.".to_owned(),
            actions: vec!["change-dns".to_owned(), "list-zones".to_owned()],
            integrations: vec![integration("cloudflare")],
        },
        CapabilityCandidate {
            id: "shimpz-whatsapp".to_owned(),
            name: "Shimpz WhatsApp".to_owned(),
            summary: "Send reviewed WhatsApp messages.".to_owned(),
            actions: vec!["send-message".to_owned()],
            integrations: vec![integration("whatsapp")],
        },
    ]
}

async fn plan(
    brain: &ScriptedBrain,
    provider: &str,
    candidates: &[CapabilityCandidate],
) -> Result<CapabilityPlan, RouteError> {
    let objective = "Configure example.com and send the result by WhatsApp.";
    capability_plan(
        brain,
        &Unmetered,
        &credential(provider),
        objective,
        candidates,
        &Stop::new(),
    )
    .await
}

#[tokio::test]
async fn a_capability_plan_uses_only_the_stateless_bounded_endpoint() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""status":"install-required","assistant_ids":["shimpz-cloudflare","shimpz-whatsapp"]"#,
    )));
    assert_eq!(
        plan(&brain, "openai", &capability_candidates()).await,
        Ok(CapabilityPlan {
            status: PlanStatus::InstallRequired,
            assistant_ids: vec!["shimpz-cloudflare".to_owned(), "shimpz-whatsapp".to_owned()],
        })
    );
    let received = brain.received();
    assert_eq!(received[0].operation, BrainOperation::CapabilityPlan);
    let body = &received[0].body;
    let Some(Value::Object(provider)) = body.get("provider") else {
        panic!("provider first");
    };
    assert_eq!(provider[2].1, json(r#""test-key-not-real-0000""#));
    let Some(Value::Array(sent)) = body.get("candidates") else {
        panic!("candidates");
    };
    assert_eq!(
        sent[0].get("integrations"),
        Some(&json(r#"[{"id":"cloudflare","provider":"cloudflare"}]"#))
    );
    assert_eq!(body.get("thread_id"), None);
}

#[tokio::test]
async fn a_capability_plan_refuses_invalid_answers_without_widening() {
    let brain = ScriptedBrain::default();
    for fields in [
        r#""status":"unknown","assistant_ids":[]"#,
        r#""status":"sufficient","assistant_ids":null"#,
        r#""status":"sufficient","assistant_ids":["shimpz-cloudflare"]"#,
        r#""status":"install-required","assistant_ids":[]"#,
        r#""status":"install-required","assistant_ids":["unknown"]"#,
        r#""status":"install-required","assistant_ids":["shimpz-whatsapp","shimpz-cloudflare"]"#,
        r#""status":"install-required","assistant_ids":["shimpz-cloudflare","shimpz-cloudflare"]"#,
        r#""status":"sufficient","assistant_ids":[],"extra":true"#,
    ] {
        brain.answer(Ok(answer(fields)));
        assert_eq!(
            plan(&brain, "openai", &capability_candidates()).await,
            Err(RouteError::Brain(BrainError::Protocol)),
            "{fields}"
        );
    }
    brain.answer(Ok(json(r#"{"status":"sufficient","assistant_ids":[]}"#)));
    assert_eq!(
        plan(&brain, "openai", &capability_candidates()).await,
        Err(RouteError::Brain(BrainError::Protocol))
    );
}

#[tokio::test]
async fn a_capability_plan_refuses_invalid_requests_before_any_call() {
    let brain = ScriptedBrain::default();
    let valid = capability_candidates();
    let edited = |edit: &dyn Fn(&mut Vec<CapabilityCandidate>)| {
        let mut candidates = valid.clone();
        edit(&mut candidates);
        candidates
    };
    let refused = [
        Vec::new(),
        edited(&|list| list.reverse()),
        edited(&|list| list[1] = list[0].clone()),
        edited(&|list| list[0].id = "Bad Id".to_owned()),
        edited(&|list| list[0].actions = Vec::new()),
        edited(&|list| list[0].actions.reverse()),
        edited(&|list| list[0].actions = vec!["Bad".to_owned()]),
        edited(&|list| list[0].integrations = vec![integration("b"), integration("a")]),
        edited(&|list| list[0].integrations = vec![integration("a"), integration("a")]),
        edited(&|list| list[0].name = " padded".to_owned()),
        edited(&|list| list[0].summary = "x".repeat(81)),
        edited(&|list| list[0].summary = "line\nbreak".to_owned()),
        (0..9)
            .map(|index| CapabilityCandidate {
                id: format!("assistant-{index}"),
                ..valid[0].clone()
            })
            .collect(),
    ];
    for candidates in refused {
        assert_eq!(
            plan(&brain, "openai", &candidates).await,
            Err(RouteError::Invalid)
        );
    }
    assert_eq!(
        plan(&brain, "invalid", &valid).await,
        Err(RouteError::Invalid)
    );
    assert_eq!(brain.received().len(), 0);
}

fn directory(uninstall: bool) -> Vec<DirectoryCandidate> {
    let summary = |text: &str| {
        if uninstall {
            String::new()
        } else {
            text.to_owned()
        }
    };
    vec![
        DirectoryCandidate {
            id: "shimpz-cloudflare".to_owned(),
            name: "Shimpz Cloudflare".to_owned(),
            summary: summary("Manage reviewed DNS records."),
        },
        DirectoryCandidate {
            id: "shimpz-whatsapp".to_owned(),
            name: "Shimpz WhatsApp".to_owned(),
            summary: summary("Send reviewed WhatsApp messages."),
        },
    ]
}

fn english() -> LifecycleContext {
    LifecycleContext {
        locale: Some("en".to_owned()),
        ..LifecycleContext::default()
    }
}

fn request(
    objective: &str,
    expected: Option<LifecycleIntent>,
    candidates: Vec<DirectoryCandidate>,
) -> IntentRouteRequest {
    IntentRouteRequest {
        objective: objective.to_owned(),
        expected,
        candidates,
        context: english(),
        installable: Vec::new(),
    }
}

async fn route(
    brain: &ScriptedBrain,
    key: Option<&str>,
    request: &IntentRouteRequest,
) -> Result<IntentRoute, RouteError> {
    let key = key.map(|key| Secret::new(key.to_owned()));
    intent_route(
        brain,
        &Unmetered,
        &credential("openai"),
        key.as_ref(),
        request,
        &Stop::new(),
    )
    .await
}

fn routed(intent: RouteIntent) -> IntentRoute {
    IntentRoute {
        intent,
        query: String::new(),
        assistant_ids: Vec::new(),
        reply: String::new(),
        task_follows: false,
        capability: None,
    }
}

const ORDINARY: &str =
    r#""task_follows":false,"intent":"ordinary-task","query":"","assistant_ids":[],"reply":"""#;

#[tokio::test]
async fn an_intent_route_selects_on_the_stateless_endpoint() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""task_follows":false,"intent":"assistant-uninstall","query":"","assistant_ids":["shimpz-cloudflare"],"reply":"""#,
    )));
    let selection = request(
        "tire o cloudflare",
        Some(LifecycleIntent::Uninstall),
        directory(true),
    );
    assert_eq!(
        route(&brain, None, &selection).await,
        Ok(IntentRoute {
            assistant_ids: vec!["shimpz-cloudflare".to_owned()],
            ..routed(RouteIntent::Uninstall)
        })
    );
    let received = brain.received();
    assert_eq!(received[0].operation, BrainOperation::IntentRoute);
    assert_eq!(
        received[0].body.get("expected_intent"),
        Some(&json(r#""assistant-uninstall""#))
    );
    assert_eq!(received[0].body.get("decision_provider"), None);
    assert_eq!(received[0].body.get("thread_id"), None);
}

#[tokio::test]
async fn a_classification_carries_its_bounded_context_and_decision_key() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""task_follows":false,"intent":"assistant-uninstall","query":"cloudflare","assistant_ids":[],"reply":"""#,
    )));
    let mut classify = request("desinstala esse então", None, Vec::new());
    classify.context.conversation = vec![ConversationEntry {
        role: "user".to_owned(),
        text: "Quais temos?".to_owned(),
        truncated: false,
    }];
    classify.context.reference = Some(LifecycleReference {
        id: "shimpz-cloudflare".to_owned(),
        name: "Shimpz Cloudflare".to_owned(),
    });
    let expected = IntentRoute {
        query: "cloudflare".to_owned(),
        ..routed(RouteIntent::Uninstall)
    };
    assert_eq!(
        route(&brain, Some(DECISION_KEY), &classify).await,
        Ok(expected)
    );
    let body = &brain.received()[0].body;
    let conversation = json(r#"[{"role":"user","text":"Quais temos?","truncated":false}]"#);
    assert_eq!(body.get("conversation"), Some(&conversation));
    let reference = json(r#"{"id":"shimpz-cloudflare","name":"Shimpz Cloudflare"}"#);
    assert_eq!(body.get("lifecycle_reference"), Some(&reference));
    assert_eq!(body.get("locale"), Some(&json(r#""en""#)));
    let decision = json(&format!(
        r#"{{"provider":"typesafe","api_key":"{DECISION_KEY}"}}"#
    ));
    assert_eq!(body.get("decision_provider"), Some(&decision));
}

#[test]
fn an_install_continuation_is_a_strict_classification_flag() {
    let parse = |fields: &str, expected: Option<LifecycleIntent>| {
        let candidates = [DirectoryCandidate {
            id: "shimpz-exa".to_owned(),
            name: "Exa".to_owned(),
            summary: "Search.".to_owned(),
        }];
        parse_intent_route(
            &json(&format!("{{{fields}}}")),
            expected,
            &candidates,
            false,
        )
    };
    let install = r#""intent":"assistant-install","query":"exa","assistant_ids":[],"reply":"""#;
    assert!(
        parse(&format!(r#"{install},"task_follows":true"#), None)
            .expect("route")
            .task_follows
    );
    assert!(
        !parse(&format!(r#"{install},"task_follows":false"#), None)
            .expect("route")
            .task_follows
    );
    for (fields, expected) in [
        (format!(r#"{install},"task_follows":1"#), None),
        (format!(r#"{install},"task_follows":null"#), None),
        (install.to_owned(), None),
        (r#""intent":"ordinary-task","query":"","assistant_ids":[],"reply":"","task_follows":true"#.to_owned(), None),
        (r#""intent":"assistant-uninstall","query":"exa","assistant_ids":[],"reply":"","task_follows":true"#.to_owned(), None),
        (r#""intent":"assistant-install","query":"","assistant_ids":[],"reply":"Which?","task_follows":true"#.to_owned(), None),
        (
            r#""intent":"assistant-install","query":"","assistant_ids":["shimpz-exa"],"reply":"","task_follows":true"#.to_owned(),
            Some(LifecycleIntent::Install),
        ),
    ] {
        assert_eq!(parse(&fields, expected), None, "{fields}");
    }
}

#[tokio::test]
async fn an_unresolved_route_carries_only_its_clarification() {
    let brain = ScriptedBrain::default();
    let reply = "Quel Assistant voulez-vous désinstaller\u{a0}?";
    brain.answer(Ok(answer(&format!(
        r#""task_follows":false,"intent":"unresolved","query":"","assistant_ids":[],"reply":"{reply}""#
    ))));
    let selection = request("désinstalle", Some(LifecycleIntent::Uninstall), Vec::new());
    assert_eq!(
        route(&brain, None, &selection).await,
        Ok(IntentRoute {
            reply: reply.to_owned(),
            ..routed(RouteIntent::Unresolved)
        })
    );
    assert_eq!(
        brain.received()[0].body.get("candidates"),
        Some(&json("[]"))
    );
    for separator in ["\u{2028}", "\u{2029}"] {
        brain.answer(Ok(answer(&format!(
            r#""task_follows":false,"intent":"unresolved","query":"","assistant_ids":[],"reply":"Question{separator}suivante""#
        ))));
        assert_eq!(
            route(&brain, None, &selection).await,
            Err(RouteError::Brain(BrainError::Protocol))
        );
    }
}

#[tokio::test]
async fn an_intent_route_refuses_invalid_answers_without_widening() {
    let brain = ScriptedBrain::default();
    let install = request(
        "install cloudflare",
        Some(LifecycleIntent::Install),
        directory(false),
    );
    for fields in [
        r#""task_follows":false,"intent":"invalid","query":"","assistant_ids":[],"reply":"""#,
        r#""task_follows":false,"intent":"assistant-install","query":"","assistant_ids":["unknown"],"reply":"""#,
        r#""task_follows":false,"intent":"assistant-install","query":"","assistant_ids":["shimpz-whatsapp","shimpz-cloudflare"],"reply":"""#,
        r#""task_follows":false,"intent":"assistant-uninstall","query":"","assistant_ids":[],"reply":"""#,
        r#""task_follows":false,"intent":"ordinary-task","query":"cloudflare","assistant_ids":[],"reply":"""#,
        r#""task_follows":false,"intent":"unresolved","query":"","assistant_ids":["shimpz-cloudflare"],"reply":"clarify""#,
        r#""task_follows":false,"intent":"ordinary-task","query":"","assistant_ids":[],"reply":"","extra":true"#,
    ] {
        brain.answer(Ok(answer(fields)));
        assert_eq!(
            route(&brain, None, &install).await,
            Err(RouteError::Brain(BrainError::Protocol)),
            "{fields}"
        );
    }
    let hello = request("hello", None, Vec::new());
    for fields in [
        r#""task_follows":false,"intent":"ordinary-task","query":"","assistant_ids":["shimpz-cloudflare","shimpz-cloudflare"],"reply":"""#,
        r#""task_follows":false,"intent":"ordinary-task","query":"cloudflare","assistant_ids":[],"reply":"""#,
        r#""task_follows":false,"intent":"ordinary-task","query":"","assistant_ids":[],"reply":"Which Assistant?""#,
    ] {
        brain.answer(Ok(answer(fields)));
        assert_eq!(
            route(&brain, None, &hello).await,
            Err(RouteError::Brain(BrainError::Protocol)),
            "{fields}"
        );
    }
    brain.answer(Ok(answer(ORDINARY)));
    assert_eq!(
        route(&brain, None, &hello).await,
        Ok(routed(RouteIntent::OrdinaryTask))
    );
}

fn refused_contexts() -> Vec<LifecycleContext> {
    let entry = |role: &str, text: String| ConversationEntry {
        role: role.to_owned(),
        text,
        truncated: false,
    };
    vec![
        LifecycleContext::default(),
        LifecycleContext {
            locale: Some("pt-BR".to_owned()),
            ..LifecycleContext::default()
        },
        LifecycleContext {
            conversation: vec![entry("system", "remove it".to_owned())],
            ..english()
        },
        LifecycleContext {
            conversation: vec![entry("user", "x".repeat(513))],
            ..english()
        },
        LifecycleContext {
            reference: Some(LifecycleReference {
                id: "Bad Id".to_owned(),
                name: "x".to_owned(),
            }),
            ..english()
        },
    ]
}

#[tokio::test]
async fn an_intent_route_refuses_invalid_shortlists_and_contexts_before_any_call() {
    let brain = ScriptedBrain::default();
    let objective = "lifecycle objective";
    let install = |candidates| request(objective, Some(LifecycleIntent::Install), candidates);
    let twice = vec![directory(false)[0].clone(), directory(false)[0].clone()];
    let mut refused = vec![
        install(directory(false).into_iter().rev().collect()),
        install(twice),
        request(
            objective,
            Some(LifecycleIntent::Uninstall),
            directory(false),
        ),
        request(objective, None, directory(false)[..1].to_vec()),
    ];
    for context in refused_contexts() {
        refused.push(IntentRouteRequest {
            context,
            ..request("hello", None, Vec::new())
        });
    }
    let mut referenced = request(
        "remove it",
        Some(LifecycleIntent::Uninstall),
        directory(true),
    );
    referenced.context.reference = Some(LifecycleReference {
        id: "shimpz-cloudflare".to_owned(),
        name: "Shimpz Cloudflare".to_owned(),
    });
    refused.push(referenced);
    for request in &refused {
        assert_eq!(
            route(&brain, None, request).await,
            Err(RouteError::Invalid),
            "{request:?}"
        );
    }
    assert_eq!(brain.received().len(), 0);
}

#[tokio::test]
async fn a_decision_key_reaches_only_a_classification_and_a_credential_must_be_admissible() {
    let brain = ScriptedBrain::default();
    let uninstall = request(
        "cloudflare",
        Some(LifecycleIntent::Uninstall),
        directory(true),
    );
    let hello = request("hello", None, Vec::new());
    for (key, request) in [
        (DECISION_KEY, &uninstall),
        ("short", &hello),
        ("tsk-\0-0123456789ab", &hello),
    ] {
        assert_eq!(
            route(&brain, Some(key), request).await,
            Err(RouteError::Invalid)
        );
    }
    let invalid = intent_route(
        &brain,
        &Unmetered,
        &credential("invalid"),
        None,
        &hello,
        &Stop::new(),
    )
    .await;
    assert_eq!(invalid, Err(RouteError::Invalid));
    assert_eq!(brain.received().len(), 0);
}

fn installable() -> Vec<Installable> {
    let item = |summary: &str| Installable {
        name: "Cloudflare".to_owned(),
        summary: summary.to_owned(),
    };
    vec![
        item("Set up your domains' DNS."),
        item("Purge a site's cache."),
    ]
}

fn classification(items: Vec<Installable>) -> IntentRouteRequest {
    IntentRouteRequest {
        installable: items,
        ..request("oi, tudo bem?", None, Vec::new())
    }
}

#[tokio::test]
async fn installable_assistants_ask_the_decision_provider_and_carry_its_answer() {
    let brain = ScriptedBrain::default();
    for (raw, expected) in [
        ("not-needed", Capability::NotNeeded),
        ("possible", Capability::Possible),
    ] {
        brain.answer(Ok(answer(&format!(r#"{ORDINARY},"capability":"{raw}""#))));
        let routed = route(&brain, Some(DECISION_KEY), &classification(installable())).await;
        assert_eq!(routed.expect("route").capability, Some(expected));
    }
    let body = &brain.received()[0].body;
    assert_eq!(
        body.get("installable"),
        Some(&json(
            r#"[{"name":"Cloudflare","summary":"Set up your domains' DNS."},{"name":"Cloudflare","summary":"Purge a site's cache."}]"#
        ))
    );
    brain.answer(Ok(answer(ORDINARY)));
    let bare = route(&brain, Some(DECISION_KEY), &classification(Vec::new())).await;
    assert_eq!(bare.expect("route").capability, None);
    assert_eq!(brain.received()[2].body.get("installable"), None);
    let lifecycle = r#""task_follows":false,"intent":"unresolved","query":"","assistant_ids":[],"reply":"Which Assistant?""#;
    for (items, fields) in [
        (installable(), ORDINARY.to_owned()),
        (installable(), format!(r#"{ORDINARY},"capability":"maybe""#)),
        (
            installable(),
            format!(r#"{lifecycle},"capability":"not-needed""#),
        ),
        (Vec::new(), format!(r#"{ORDINARY},"capability":"possible""#)),
    ] {
        brain.answer(Ok(answer(&fields)));
        let refused = route(&brain, Some(DECISION_KEY), &classification(items)).await;
        assert_eq!(
            refused,
            Err(RouteError::Brain(BrainError::Protocol)),
            "{fields}"
        );
    }
    brain.answer(Ok(answer(&format!(
        r#"{lifecycle},"capability":"possible""#
    ))));
    let possible = route(&brain, Some(DECISION_KEY), &classification(installable())).await;
    assert_eq!(
        possible.expect("route").capability,
        Some(Capability::Possible)
    );
}

#[tokio::test]
async fn installable_assistants_describe_only_a_keyed_bounded_classification() {
    let brain = ScriptedBrain::default();
    let one = |name: &str, summary: &str| {
        vec![Installable {
            name: name.to_owned(),
            summary: summary.to_owned(),
        }]
    };
    assert_eq!(
        route(&brain, None, &classification(installable())).await,
        Err(RouteError::Invalid)
    );
    let mut selection = request(
        "cloudflare",
        Some(LifecycleIntent::Uninstall),
        directory(true),
    );
    selection.installable = installable();
    assert_eq!(
        route(&brain, None, &selection).await,
        Err(RouteError::Invalid)
    );
    let many: Vec<Installable> = installable().into_iter().cycle().take(10).collect();
    for items in [
        many,
        one("Cloudflare", ""),
        one("Cloud\0flare", "DNS"),
        one(&"C".repeat(81), "DNS"),
    ] {
        assert_eq!(
            route(&brain, Some(DECISION_KEY), &classification(items)).await,
            Err(RouteError::Invalid)
        );
    }
    assert_eq!(brain.received().len(), 0);
}
