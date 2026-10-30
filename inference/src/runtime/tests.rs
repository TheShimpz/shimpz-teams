use std::fmt::Write as _;
use std::sync::Mutex;

use strict_json::Value;
use team_core::secret::Secret;
use team_core::stop::Stop;

use super::{
    ActionRequest, Brain, ConversationEntry, MAX_REQUEST_BYTES, RESULT_KEY_BYTES, Runtime,
    RuntimeAction, RuntimeAssistant, RuntimeContext, TurnStatus, contract_digest, resume_capacity,
};
use crate::brain::{BrainError, BrainOperation, ModelCredential};
use crate::testing::ScriptedBrain;
use crate::usage::{Counts, UsageRecorder};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

const NO_USAGE: &str = r#""usage":{"model_calls":1,"provider_requests":1,"failed_calls":0,"unreported_calls":0,
"input_tokens":7,"output_tokens":3,"cache_read_tokens":0,"cache_write_tokens":0}"#;

/// A turn answer with its usage, memory, Routine, and Browser defaults unless `fields` states them.
fn answer(fields: &str) -> Value {
    let mut text = format!("{{{fields}");
    for (name, default) in [
        ("memory", "[]"),
        ("routine", "null"),
        ("browser", "null"),
        ("clarification", "null"),
    ] {
        if !fields.contains(&format!("\"{name}\"")) {
            let _ = write!(text, ",\"{name}\":{default}");
        }
    }
    text.push(',');
    text.push_str(NO_USAGE);
    text.push('}');
    json(&text)
}

fn context() -> RuntimeContext {
    RuntimeContext {
        thread_id: "team:hello-pulse:conversation-1".to_owned(),
        team_name: "Marketing".to_owned(),
        assistants: vec![RuntimeAssistant {
            id: "hello-pulse".to_owned(),
            genesis: "Combine the declared greeting Actions into one bounded welcome.".to_owned(),
            actions: vec![RuntimeAction {
                id: "hello".to_owned(),
                summary: "Return a greeting.".to_owned(),
                input_schema: json(
                    r#"{"type":"object","properties":{"name":{"type":"string"}},"additionalProperties":false}"#,
                ),
                authorization: false,
                input_files: Vec::new(),
                output_schema: json("{}"),
            }],
        }],
        credential: ModelCredential {
            provider: "openai".to_owned(),
            model: "gpt-test".to_owned(),
            api_key: Secret::new("test-key-not-real-0000".to_owned()),
        },
        memories: None,
        routines: None,
        routine_question: None,
        routine_mode: false,
        routine_rerun: None,
        routine_schedule: None,
        routine_output: None,
        routine_settings_answer: false,
        routine_selected: None,
        locale: None,
        turn_clock: None,
        attachments: Vec::new(),
        browser: false,
    }
}

#[derive(Default)]
struct Recorded(Mutex<Vec<(&'static str, String, String, Counts)>>);

impl UsageRecorder for Recorded {
    fn record(&self, operation: &'static str, provider: &str, model: &str, counts: &Counts) {
        self.0.lock().expect("usage").push((
            operation,
            provider.to_owned(),
            model.to_owned(),
            *counts,
        ));
    }
}

async fn start(
    brain: &ScriptedBrain,
    context: &RuntimeContext,
    message: &str,
) -> Result<super::RuntimeTurn, BrainError> {
    let usage = Recorded::default();
    let runtime = Brain {
        client: brain,
        usage: &usage,
    };
    runtime.start(context, message, &[], &Stop::new()).await
}

fn sent(brain: &ScriptedBrain) -> Value {
    brain.received().last().expect("a call").body.clone()
}

#[tokio::test]
async fn start_carries_the_context_and_records_the_turn_usage() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""status":"completed","reply":"Hello.","actions":[]"#,
    )));
    let usage = Recorded::default();
    let runtime = Brain {
        client: &brain,
        usage: &usage,
    };
    let window = [
        ConversationEntry {
            role: "user".to_owned(),
            text: "List my DNS zones".to_owned(),
            truncated: false,
        },
        ConversationEntry {
            role: "assistant".to_owned(),
            text: "Install Cloudflare first.".to_owned(),
            truncated: false,
        },
    ];
    let turn = runtime
        .start(&context(), "Hello", &window, &Stop::new())
        .await
        .expect("turn");
    assert_eq!(turn.status, TurnStatus::Completed);
    assert_eq!(turn.reply, "Hello.");
    let received = brain.received();
    assert_eq!(received[0].operation, BrainOperation::Turn);
    assert_eq!(received[0].provider.as_deref(), Some("openai"));
    let body = &received[0].body;
    assert_eq!(body.get("team_name"), Some(&json("\"Marketing\"")));
    assert_eq!(body.get("provider"), None);
    assert_eq!(
        body.get("conversation"),
        Some(&json(
            r#"[{"role":"user","text":"List my DNS zones","truncated":false},
            {"role":"assistant","text":"Install Cloudflare first.","truncated":false}]"#
        ))
    );
    assert_eq!(body.get("turn_clock"), None);
    assert_eq!(body.get("attachments"), Some(&json("[]")));
    let recorded = usage.0.lock().expect("usage").clone();
    assert_eq!(recorded.len(), 1);
    assert_eq!(
        (recorded[0].0, recorded[0].2.as_str()),
        ("turn", "gpt-test")
    );
    assert_eq!(recorded[0].3.input_tokens, 7);
}

#[tokio::test]
async fn only_a_start_carries_the_turn_clock_and_resume_carries_only_results() {
    let brain = ScriptedBrain::default();
    let done = || {
        Ok(answer(
            r#""status":"completed","reply":"Done.","actions":[]"#,
        ))
    };
    let mut clocked = context();
    clocked.turn_clock = Some(("2026-10-06".to_owned(), "America/Sao_Paulo".to_owned()));
    brain.answer(done());
    start(&brain, &clocked, "Hello").await.expect("turn");
    assert_eq!(
        sent(&brain).get("turn_clock"),
        Some(&json(
            r#"{"date":"2026-10-06","timezone":"America/Sao_Paulo"}"#
        ))
    );
    brain.answer(done());
    let usage = Recorded::default();
    let runtime = Brain {
        client: &brain,
        usage: &usage,
    };
    runtime
        .resume(
            &clocked,
            json(r#"{"interrupt-1":{"status":"ok"}}"#),
            &Stop::new(),
        )
        .await
        .expect("turn");
    let received = brain.received();
    assert_eq!(received[1].operation, BrainOperation::Resume);
    let body = &received[1].body;
    assert_eq!(body.get("turn_clock"), None);
    assert_eq!(body.get("conversation"), None);
    assert_eq!(
        body.get("results"),
        Some(&json(r#"{"interrupt-1":{"status":"ok"}}"#))
    );
    assert_eq!(usage.0.lock().expect("usage")[0].0, "turn-resume");
}

#[tokio::test]
async fn the_brain_gets_attachments_action_gates_and_output_schemas() {
    let attachment = json(
        r#"{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"notes.md","media_type":"text/markdown","size":5,
        "sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "content":{"type":"text","text":"notes","pdf":false}}"#,
    );
    let mut gated = context();
    gated.attachments = vec![attachment.clone()];
    gated.assistants[0].actions = vec![RuntimeAction {
        id: "upload".to_owned(),
        summary: "Upload.".to_owned(),
        input_schema: json(r#"{"type":"object"}"#),
        authorization: true,
        input_files: vec!["document".to_owned()],
        output_schema: json("{}"),
    }];
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""status":"completed","reply":"Done.","actions":[]"#,
    )));
    start(&brain, &gated, "Use my notes").await.expect("turn");
    let body = sent(&brain);
    assert_eq!(
        body.get("attachments"),
        Some(&Value::Array(vec![attachment]))
    );
    let action = &body.get("assistants").and_then(|list| match list {
        Value::Array(items) => items.first().and_then(|assistant| assistant.get("actions")),
        _ => None,
    });
    assert_eq!(
        *action,
        Some(&json(
            r#"[{"id":"upload","summary":"Upload.","input_schema":{"type":"object"},"output_schema":{},
            "authorization":true,"input_files":["document"]}]"#
        ))
    );
    let assistant = gated.assistants[0].clone();
    let mut outputs = assistant.clone();
    outputs.actions[0].output_schema =
        json(r#"{"type":"object","properties":{"id":{"type":"string"}}}"#);
    assert_ne!(contract_digest(&assistant), contract_digest(&outputs));
    assert!(contract_digest(&assistant).starts_with("sha256:"));
}

#[tokio::test]
async fn an_invalid_conversation_or_locale_is_refused_before_any_request() {
    let brain = ScriptedBrain::default();
    let usage = Recorded::default();
    let runtime = Brain {
        client: &brain,
        usage: &usage,
    };
    let entry = |role: &str| ConversationEntry {
        role: role.to_owned(),
        text: "x".to_owned(),
        truncated: false,
    };
    let oversized: Vec<_> = (0..9).map(|_| entry("user")).collect();
    for window in [oversized, vec![entry("system")]] {
        let refused = runtime
            .start(&context(), "Hello", &window, &Stop::new())
            .await;
        assert_eq!(refused, Err(BrainError::Protocol));
    }
    let mut foreign = context();
    foreign.locale = Some("xx".to_owned());
    assert_eq!(
        runtime.start(&foreign, "Hello", &[], &Stop::new()).await,
        Err(BrainError::Protocol)
    );
    assert_eq!(brain.received().len(), 0);
}

#[tokio::test]
async fn an_action_suspension_is_parsed_without_gaining_execution_authority() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""status":"action-required","reply":"","actions":[{"interrupt_id":"interrupt-1",
        "assistant_id":"hello-pulse","action":"hello","input":{"name":"Ada"}}]"#,
    )));
    let turn = start(&brain, &context(), "Greet Ada").await.expect("turn");
    assert_eq!(
        turn.actions,
        vec![ActionRequest {
            interrupt_id: "interrupt-1".to_owned(),
            assistant_id: "hello-pulse".to_owned(),
            action: "hello".to_owned(),
            input: json(r#"{"name":"Ada"}"#),
        }]
    );
}

#[tokio::test]
async fn a_completed_turn_carries_one_closed_clarification() {
    let asked = r#"{"question":"Qual período?","options":[{"label":"Hoje","description":""},
        {"label":"Semana","description":"Sete dias."}],"default_index":1}"#;
    let rendered = "Qual período?\\n\\n1. Hoje\\n2. Semana ✓ — Sete dias.";
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(&format!(
        r#""status":"completed","clarification":{asked},"reply":"{rendered}","actions":[]"#
    ))));
    let turn = start(&brain, &context(), "Quais modelos?")
        .await
        .expect("turn");
    assert_eq!(turn.clarification, Some(json(asked)));
    brain.answer(Ok(answer(&format!(
        r#""status":"completed","clarification":{asked},"reply":"I deleted everything.","actions":[]"#
    ))));
    assert_eq!(
        start(&brain, &context(), "Quais?").await,
        Err(BrainError::Protocol)
    );
}

#[tokio::test]
async fn a_completed_turn_carries_at_most_one_routine_record_and_never_with_a_question() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""status":"completed","reply":"Ok.","actions":[],"routine":{"name":"DNS semanal"}"#,
    )));
    let mut chat = context();
    chat.routines = Some(vec![json(r#"{"routine_id":"r"}"#)]);
    chat.routine_mode = true;
    chat.routine_output = Some("changes".to_owned());
    let turn = start(&brain, &chat, "Toda segunda").await.expect("turn");
    assert_eq!(turn.routine, Some(json(r#"{"name":"DNS semanal"}"#)));
    let body = sent(&brain);
    assert_eq!(body.get("routines"), Some(&json(r#"[{"routine_id":"r"}]"#)));
    assert_eq!(body.get("routine_mode"), Some(&Value::Bool(true)));
    assert_eq!(body.get("routine_output"), Some(&json("\"changes\"")));
    brain.answer(Ok(answer(
        r#""status":"completed","reply":"","actions":[],"routine":{"name":"DNS semanal"}"#,
    )));
    assert_eq!(start(&brain, &chat, "x").await.expect("turn").reply, "");
    let question = r#"{"question":"Qual?","options":[{"label":"A","description":""},{"label":"B","description":""}],"default_index":0}"#;
    for fields in [
        r#""status":"completed","reply":"Ok.","actions":[],"routine":["record"]"#.to_owned(),
        r#""status":"completed","reply":" ","actions":[],"routine":{"name":"x"}"#.to_owned(),
        r#""status":"action-required","reply":"","actions":[{"interrupt_id":"i","assistant_id":"a","action":"b","input":{}}],"routine":{"name":"x"}"#.to_owned(),
        format!(
            r#""status":"completed","clarification":{question},"reply":"Qual?\n\n1. A ✓\n2. B","actions":[],"routine":{{"name":"x"}}"#
        ),
    ] {
        brain.answer(Ok(answer(&fields)));
        assert_eq!(start(&brain, &context(), "Toda segunda").await, Err(BrainError::Protocol), "{fields}");
    }
}

#[tokio::test]
async fn malformed_turn_answers_fail_closed() {
    let long = "x".repeat(60_001);
    let invalid = [
        r#""status":"completed","reply":"","actions":[]"#.to_owned(),
        format!(r#""status":"completed","reply":"{long}","actions":[]"#),
        r#""status":"completed","reply":"unsafe\u0000reply","actions":[]"#.to_owned(),
        r#""status":"completed","reply":"ok","actions":[{"action":"hello"}]"#.to_owned(),
        r#""status":"action-required","reply":"unexpected","actions":[]"#.to_owned(),
        r#""status":"unknown","reply":"ok","actions":[]"#.to_owned(),
        r#""status":"completed","clarification":{"question":"Q?"},"reply":"ok","actions":[]"#.to_owned(),
        r#""status":"action-required","clarification":{"question":"Qual?","options":[{"label":"A","description":""},{"label":"B","description":""}],"default_index":0},"reply":"","actions":[{"interrupt_id":"i-1","assistant_id":"hello-pulse","action":"hello","input":{}}]"#.to_owned(),
        r#""status":"completed","reply":"ok","actions":[],"extra":true"#.to_owned(),
        r#""status":"action-required","reply":"","actions":[{"interrupt_id":"bad interrupt","assistant_id":"hello-pulse","action":"hello","input":{}}]"#.to_owned(),
        r#""status":"completed","reply":"ok","actions":[],"memory":[{"op":"remember"}]"#.to_owned(),
        r#""status":"action-required","reply":"","actions":[{"interrupt_id":"i","assistant_id":"hello-pulse","action":"hello","input":{}}],"memory":[{"op":"forget","topic":"tone","preference":""}]"#.to_owned(),
    ];
    let brain = ScriptedBrain::default();
    for fields in invalid {
        brain.answer(Ok(answer(&fields)));
        assert_eq!(
            start(&brain, &context(), "Hello").await,
            Err(BrainError::Protocol),
            "{fields}"
        );
    }
    // A missing clarification member is a missing field, and an answer without usage is refused.
    brain.answer(Ok(json(&format!(
        r#"{{"status":"completed","reply":"ok","actions":[],"memory":[],"routine":null,"browser":null,{NO_USAGE}}}"#
    ))));
    assert_eq!(
        start(&brain, &context(), "Hello").await,
        Err(BrainError::Protocol)
    );
    brain.answer(Ok(json(
        r#"{"status":"completed","reply":"ok","actions":[],"memory":[],"routine":null,"browser":null,"clarification":null}"#,
    )));
    assert_eq!(
        start(&brain, &context(), "Hello").await,
        Err(BrainError::Protocol)
    );
    brain.answer(Ok(json("[]")));
    assert_eq!(
        start(&brain, &context(), "Hello").await,
        Err(BrainError::Protocol)
    );
}

#[tokio::test]
async fn reported_usage_is_recorded_even_when_the_rest_is_refused() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""status":"unknown","reply":"ok","actions":[]"#,
    )));
    let usage = Recorded::default();
    let runtime = Brain {
        client: &brain,
        usage: &usage,
    };
    assert_eq!(
        runtime.start(&context(), "Hello", &[], &Stop::new()).await,
        Err(BrainError::Protocol)
    );
    assert_eq!(usage.0.lock().expect("usage").len(), 1);
}

#[tokio::test]
async fn a_browser_call_is_admitted_only_alone() {
    let brain = ScriptedBrain::default();
    brain.answer(Ok(answer(
        r#""status":"browser-required","reply":"","actions":[],"browser":{"interrupt_id":"b-1","objective":"Find the price of a flight."}"#,
    )));
    let turn = start(&brain, &context(), "Find").await.expect("turn");
    assert_eq!(turn.status, TurnStatus::BrowserRequired);
    assert_eq!(turn.browser.expect("browser").interrupt_id, "b-1");
    for fields in [
        r#""status":"browser-required","reply":"x","actions":[],"browser":{"interrupt_id":"b-1","objective":"Find it."}"#,
        r#""status":"browser-required","reply":"","actions":[],"browser":null"#,
        r#""status":"completed","reply":"ok","actions":[],"browser":{"interrupt_id":"b-1","objective":"Find it."}"#,
        r#""status":"browser-required","reply":"","actions":[],"browser":{"interrupt_id":"b-1","objective":" Find it."}"#,
    ] {
        brain.answer(Ok(answer(fields)));
        assert_eq!(
            start(&brain, &context(), "Find").await,
            Err(BrainError::Protocol),
            "{fields}"
        );
    }
}

#[tokio::test]
async fn a_purpose_is_optional_and_any_failure_is_absent() {
    let brain = ScriptedBrain::default();
    let usage = Recorded::default();
    let runtime = Brain {
        client: &brain,
        usage: &usage,
    };
    let request = ActionRequest {
        interrupt_id: "i-1".to_owned(),
        assistant_id: "hello-pulse".to_owned(),
        action: "hello".to_owned(),
        input: json("{}"),
    };
    let stop = Stop::new();
    brain.answer(Ok(json(&format!(
        r#"{{"purpose":"To greet Ada.",{NO_USAGE}}}"#
    ))));
    assert_eq!(
        runtime
            .purpose(&context(), &request, "Hello", "Greets.", &stop)
            .await,
        Some("To greet Ada.".to_owned())
    );
    let body = sent(&brain);
    assert_eq!(body.get("action_id"), Some(&json("\"hello\"")));
    assert_eq!(
        body.get("thread_id"),
        Some(&json("\"team:hello-pulse:conversation-1\""))
    );
    for failed in [
        Ok(json(&format!(r#"{{"purpose":null,{NO_USAGE}}}"#))),
        Ok(json(&format!(r#"{{"purpose":"x","extra":1,{NO_USAGE}}}"#))),
        Ok(json(r#"{"purpose":"To greet."}"#)),
        Err(BrainError::Unavailable),
    ] {
        brain.answer(failed);
        assert_eq!(
            runtime
                .purpose(&context(), &request, "Hello", "Greets.", &stop)
                .await,
            None
        );
    }
}

#[tokio::test]
async fn delete_thread_is_closed_both_ways() {
    let brain = ScriptedBrain::default();
    let usage = Recorded::default();
    let runtime = Brain {
        client: &brain,
        usage: &usage,
    };
    let stop = Stop::new();
    brain.answer(Ok(json(r#"{"status":"deleted"}"#)));
    assert_eq!(runtime.delete_thread("team:t:1", &stop).await, Ok(()));
    assert_eq!(
        brain.received()[0].body,
        json(r#"{"thread_id":"team:t:1"}"#)
    );
    assert_eq!(brain.received()[0].provider, None);
    assert_eq!(
        runtime.delete_thread("bad id", &stop).await,
        Err(BrainError::Protocol)
    );
    assert_eq!(brain.received().len(), 1);
    for answer in [
        r#"{"status":"ok"}"#,
        r#"{"status":"deleted","thread_id":"t"}"#,
    ] {
        brain.answer(Ok(json(answer)));
        assert_eq!(
            runtime.delete_thread("team:t:1", &stop).await,
            Err(BrainError::Protocol)
        );
    }
}

#[test]
fn resume_capacity_reserves_each_results_worst_case_beside_the_exact_context() {
    let small = resume_capacity(&context(), 1024);
    let per_result = 1024 + RESULT_KEY_BYTES;
    assert!(small > 0 && small * per_result <= MAX_REQUEST_BYTES);
    let mut large = context();
    large.attachments = vec![Value::String(
        "x".repeat(MAX_REQUEST_BYTES - 10 * per_result),
    )];
    let capacity = resume_capacity(&large, 1024);
    assert!(capacity < 10 && capacity > 0, "{capacity}");
    let mut full = context();
    full.attachments = vec![Value::String("x".repeat(MAX_REQUEST_BYTES))];
    assert_eq!(resume_capacity(&full, 1024), 0);
}
