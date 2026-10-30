use strict_json::Value;
use team_protocol::payload::{MAX_TURN_DURATION_MS, MAX_TURN_USAGE_MODELS, MAX_TURN_USAGE_TOKENS};

use super::{Counts, MAX_COUNT, Spent, Tokens, TurnUsage, add_tokens, parse};

const USAGE: &str = r#"{"model_calls":2,"provider_requests":3,"failed_calls":0,"unreported_calls":1,
"input_tokens":900,"output_tokens":40,"cache_read_tokens":512,"cache_write_tokens":0}"#;

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn with(name: &str, value: &str) -> Value {
    let Value::Object(mut members) = json(USAGE) else {
        unreachable!()
    };
    for member in &mut members {
        if member.0 == name {
            member.1 = json(value);
        }
    }
    Value::Object(members)
}

#[test]
fn only_the_closed_consistent_shape_parses() {
    assert_eq!(
        parse(&json(USAGE)),
        Some(Counts {
            model_calls: 2,
            provider_requests: 3,
            failed_calls: 0,
            unreported_calls: 1,
            input_tokens: 900,
            output_tokens: 40,
            cache_read_tokens: 512,
            cache_write_tokens: 0,
        })
    );
    let Value::Object(members) = json(USAGE) else {
        unreachable!()
    };
    let missing = Value::Object(members[..7].to_vec());
    let mut extra = members.clone();
    extra.push(("usd".to_owned(), json("1")));
    let broken = [
        Value::Null,
        json("[]"),
        missing,
        Value::Object(extra),
        with("input_tokens", "true"),
        with("input_tokens", "1.0"),
        with("output_tokens", "-1"),
        with("provider_requests", "-1"),
        with("provider_requests", "\"3\""),
        with("output_tokens", &(MAX_COUNT + 1).to_string()),
        with("failed_calls", "2"),
    ];
    for value in broken {
        assert_eq!(parse(&value), None, "{value:?}");
    }
}

fn reported(inputs: u64, outputs: u64) -> Counts {
    Counts {
        model_calls: 1,
        provider_requests: 1,
        input_tokens: inputs,
        output_tokens: outputs,
        cache_write_tokens: 1328,
        ..Counts::default()
    }
}

#[test]
fn the_request_tally_keeps_only_reported_calls() {
    let mut tokens = Tokens::new();
    add_tokens(&mut tokens, "openai", "gpt-6-luna", &reported(1331, 36));
    add_tokens(&mut tokens, "openai", "gpt-6-luna", &reported(9, 4));
    add_tokens(
        &mut tokens,
        "anthropic",
        "claude-sonnet-5-5",
        &Counts::default(),
    );
    assert_eq!(
        tokens.into_iter().collect::<Vec<_>>(),
        vec![(("openai".to_owned(), "gpt-6-luna".to_owned()), (1340, 40))]
    );
}

fn model(provider: &str, model: &str, inputs: i64, outputs: i64) -> (String, String, i64, i64) {
    (provider.to_owned(), model.to_owned(), inputs, outputs)
}

#[test]
fn joining_adds_the_current_request_to_the_carried_turn_within_the_wire_bounds() {
    let started = TurnUsage {
        started_ms: 1_000,
        models: vec![model("openai", "gpt-6-luna", 10, 2)],
    };
    assert_eq!(started.joined(&Tokens::new()), started);
    let mut used = Tokens::new();
    add_tokens(&mut used, "openai", "gpt-6-luna", &reported(1331, 36));
    add_tokens(
        &mut used,
        "anthropic",
        "claude-sonnet-5-5",
        &reported(1331, 36),
    );
    let joined = started.joined(&used);
    assert_eq!(
        joined.models,
        vec![
            model("anthropic", "claude-sonnet-5-5", 1331, 36),
            model("openai", "gpt-6-luna", 1341, 38)
        ]
    );
    assert_eq!(joined.started_ms, 1_000);

    let maximum = MAX_TURN_USAGE_TOKENS;
    let mut models = vec![
        model("Upper", "model", 1, 1),
        model("openai", "models/gpt", 1, 1),
        model("openai", "gpt-6-luna", maximum, maximum - 1),
    ];
    models.extend((0..20).map(|index| model(&format!("provider-{index:02}"), "model", 1, 1)));
    let crowded = TurnUsage {
        started_ms: 0,
        models,
    };
    let mut used = Tokens::new();
    add_tokens(&mut used, "openai", "gpt-6-luna", &reported(1331, 36));
    let bounded = crowded.joined(&used);
    assert_eq!(bounded.models.len(), MAX_TURN_USAGE_MODELS);
    assert_eq!(
        bounded.models[0],
        model("openai", "gpt-6-luna", maximum, maximum)
    );
    assert!(
        bounded
            .models
            .iter()
            .all(|entry| entry.0 != "Upper" && entry.1 != "models/gpt")
    );
    assert_eq!(bounded.models[MAX_TURN_USAGE_MODELS - 1].0, "provider-14");
}

#[test]
fn the_wire_value_is_absent_without_calls_and_clamps_its_duration() {
    let started = TurnUsage::start(5_000);
    assert_eq!(started.measured(6_000).wire(), None);
    let used = TurnUsage {
        started_ms: 5_000,
        models: vec![model("openai", "gpt-6-luna", 1331, 36)],
    };
    assert_eq!(
        used.measured(11_200).wire(),
        Some(json(
            r#"{"duration_ms":6200,"models":[{"provider":"openai","model":"gpt-6-luna","input_tokens":1331,"output_tokens":36}]}"#
        ))
    );
    let duration = |spent: Spent| spent.duration_ms;
    assert_eq!(duration(used.measured(4_000)), 0);
    assert_eq!(
        duration(used.measured(5_000 + MAX_TURN_DURATION_MS + 1)),
        MAX_TURN_DURATION_MS
    );
}

#[test]
fn several_turns_total_within_the_wire_bounds() {
    let first = Spent {
        duration_ms: MAX_TURN_DURATION_MS,
        models: vec![model("openai", "gpt-6-luna", 1, 2)],
    };
    let second = Spent {
        duration_ms: 5,
        models: vec![
            model("openai", "gpt-6-luna", 3, 4),
            model("anthropic", "claude-sonnet-5-5", 1, 1),
        ],
    };
    assert_eq!(
        Spent::total(&[first, second]),
        Spent {
            duration_ms: MAX_TURN_DURATION_MS,
            models: vec![
                model("anthropic", "claude-sonnet-5-5", 1, 1),
                model("openai", "gpt-6-luna", 4, 6)
            ],
        }
    );
}
