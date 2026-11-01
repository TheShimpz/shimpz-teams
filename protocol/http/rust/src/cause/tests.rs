use strict_json::Value;

use super::{
    CAUSE_MEMBERS, Cause, Cleanup, Condition, Kind, MAX_CAUSE_BYTES, MAX_CAUSE_COUNT,
    MAX_RENDERED_CHARS, Phase, UNKNOWN_CAUSE, canonical_cause, render_cause,
};
use crate::python::json::{self, Options};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

/// A cause of every condition, each with the parameters it requires.
fn every_cause() -> Vec<Cause> {
    Condition::ALL
        .iter()
        .map(|condition| match condition {
            Condition::ExitStatus => Cause::exit_status(1),
            Condition::StderrOutput => Cause::stderr_output(12),
            Condition::OutputTooLarge => Cause::output_too_large(524_289, Phase::Exchange),
            Condition::HandledFailure => Cause::handled(Some(429)),
            other => Cause::new(*other, Phase::Exchange),
        })
        .collect()
}

#[test]
fn every_condition_round_trips_through_its_canonical_object() {
    for cause in every_cause() {
        let value = cause.to_value();
        assert_eq!(Cause::from_value(&value), Some(cause), "{value:?}");
        assert_eq!(canonical_cause(&value), Some(value));
    }
}

#[test]
fn the_largest_cause_stays_within_its_serialized_and_rendered_bounds() {
    let compact = Options {
        ensure_ascii: true,
        sort_keys: false,
        compact: true,
    };
    let longest_condition = Condition::ALL
        .iter()
        .copied()
        .max_by_key(|condition| condition.as_str().len() + condition.kind().as_str().len())
        .expect("a condition");
    for base in [
        Cause::exit_status(i64::MIN).with_stderr(usize::MAX),
        Cause::output_too_large(usize::MAX, Phase::ResultValidation),
        Cause::handled(Some(599)),
        Cause::new(longest_condition, Phase::ResultValidation),
    ] {
        let cause = base.with_cleanup(Cleanup::WorkloadStopUnconfirmed);
        assert!(json::dumps(&cause.to_value(), compact).len() <= MAX_CAUSE_BYTES);
        assert!(cause.render().chars().count() <= MAX_RENDERED_CHARS);
    }
}

#[test]
fn a_cause_renders_its_words_parameters_and_cleanup_and_none_renders_as_unknown() {
    let cause = Cause::exit_status(137)
        .with_stderr(48)
        .with_cleanup(Cleanup::WorkloadStopped);
    assert_eq!(
        cause.render(),
        "assistant-exit · exit-status · exit-inspection · exit-code 137 · stderr-bytes 48 · workload-stopped"
    );
    assert_eq!(render_cause(None), UNKNOWN_CAUSE);
    let audit = Cause::new(Condition::RecordRefused, Phase::ProviderCall);
    assert_eq!(
        render_cause(Some(&audit)),
        "team-audit · record-refused · provider-call"
    );
}

#[test]
fn a_kind_names_exactly_the_conditions_it_owns() {
    for kind in Kind::ALL {
        assert!(
            Condition::ALL
                .iter()
                .any(|condition| condition.kind() == *kind),
            "{kind:?} owns no condition"
        );
    }
    assert_eq!(CAUSE_MEMBERS.len(), 8);
}

#[test]
fn parameters_appear_exactly_where_their_condition_admits_them() {
    assert_eq!(
        Cause::new(Condition::Stopped, Phase::Exchange).with_stderr(5),
        Cause::new(Condition::Stopped, Phase::Exchange)
    );
    assert_eq!(
        Cause::handled(Some(700)).to_value(),
        Cause::handled(None).to_value()
    );
    assert_eq!(
        Cause::output_too_large(usize::MAX, Phase::Exchange)
            .to_value()
            .get("bytes"),
        Some(&Value::Number(strict_json::Number::Int(MAX_CAUSE_COUNT)))
    );
}

#[test]
fn every_other_object_is_refused() {
    for invalid in [
        "{}",
        "[]",
        r#""timeout""#,
        r#"{"kind":"assistant-timeout","condition":"deadline-passed"}"#,
        r#"{"kind":"assistant-exit","condition":"deadline-passed","phase":"exchange"}"#,
        r#"{"kind":"assistant-timeout","condition":"deadline-passed","phase":"later"}"#,
        r#"{"kind":"assistant-timeout","condition":"timeout","phase":"exchange"}"#,
        r#"{"kind":"assistant-timeout","condition":"deadline-passed","phase":"exchange","stderr":"Traceback"}"#,
        r#"{"kind":"assistant-timeout","condition":"deadline-passed","phase":"exchange","cleanup":"maybe"}"#,
        r#"{"kind":"assistant-timeout","condition":"deadline-passed","phase":"exchange","exit_code":1}"#,
        r#"{"kind":"assistant-exit","condition":"exit-status","phase":"exit-inspection"}"#,
        r#"{"kind":"assistant-exit","condition":"exit-status","phase":"exit-inspection","exit_code":"1"}"#,
        r#"{"kind":"assistant-exit","condition":"exit-status","phase":"exit-inspection","exit_code":1.0}"#,
        r#"{"kind":"assistant-exit","condition":"exit-status","phase":"exit-inspection","exit_code":4294967296}"#,
        r#"{"kind":"assistant-exit","condition":"stderr-output","phase":"exit-inspection"}"#,
        r#"{"kind":"assistant-exit","condition":"stderr-output","phase":"exit-inspection","stderr_bytes":0}"#,
        r#"{"kind":"assistant-output-invalid","condition":"output-too-large","phase":"exchange"}"#,
        r#"{"kind":"assistant-output-invalid","condition":"output-too-large","phase":"exchange","bytes":-1}"#,
        r#"{"kind":"assistant-output-invalid","condition":"line-not-json","phase":"exchange","stderr_bytes":3}"#,
        r#"{"kind":"assistant-answer","condition":"handled-failure","phase":"result-validation","http_status":99}"#,
        r#"{"kind":"team-audit","condition":"record-refused","phase":"provider-call","http_status":500}"#,
        r#"{"kind":"assistant-output-invalid","condition":"result-schema-mismatch","phase":"result-validation","member":"api_key"}"#,
    ] {
        assert_eq!(canonical_cause(&json(invalid)), None, "{invalid}");
    }
}

#[test]
fn a_cause_written_with_sorted_members_reads_as_its_canonical_object() {
    let sorted = json(
        r#"{"cleanup":"workload-stopped","condition":"record-refused","kind":"team-audit","phase":"provider-call"}"#,
    );
    let cause = Cause::new(Condition::RecordRefused, Phase::ProviderCall)
        .with_cleanup(Cleanup::WorkloadStopped);
    assert_eq!(canonical_cause(&sorted), Some(cause.to_value()));
}
