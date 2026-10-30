//! `test_routine_provenance.py`: each recorded input is a literal, the run date, a copied source, or a question, never
//! a guess.

use strict_json::Value;

use super::{SHIMPZ_ID, STARTED_AT, Span, call_value, last_input, records, send, then_hourly};
use crate::json::{at, int, object, text};
use crate::recording::{Asked, Question, RERUN, Recorded, Target};
use crate::testing::{epoch, json};

/// The work's last call sends `value` as its `match` member, after a fetch of each earlier result; the person's
/// request is `known`, answered every hour.
fn span(value: &Value, known: &str, earlier: &[Value]) -> Span {
    let mut calls: Vec<_> = earlier
        .iter()
        .enumerate()
        .map(|(index, item)| {
            let input = object(vec![("call", int(i64::try_from(index).expect("an index")))]);
            call_value("reports/fetch", &input, item)
        })
        .collect();
    let input = object(vec![("zone_id", text("z")), ("match", value.clone())]);
    calls.push(call_value(
        "cloudflare/list-dns-records",
        &input,
        &json("{}"),
    ));
    Span::of(vec![send(calls, &then_hourly(known))])
}

fn origin_of(recorded: &Recorded) -> String {
    let steps = crate::json::elements(at(&recorded.document, "steps"));
    let last = crate::json::as_str(at(&steps[steps.len() - 1], "id"))
        .expect("an id")
        .to_owned();
    recorded
        .origin(&last, "match")
        .expect("an origin")
        .to_owned()
}

fn classify(value: &Value, known: &str, earlier: &[Value]) -> (Value, String) {
    let recorded = span(value, known, earlier).recorded();
    (
        at(last_input(&recorded), "match").clone(),
        origin_of(&recorded),
    )
}

fn classify_at(
    value: &str,
    known: &str,
    earlier: &[Value],
    started_at: i64,
    timezone: &str,
) -> (Value, String) {
    let mut span = span(&text(value), known, earlier);
    span.sends[0].started_at = started_at;
    span.sends[0].timezone = Some(timezone.to_owned());
    let recorded = span.recorded();
    (
        at(last_input(&recorded), "match").clone(),
        origin_of(&recorded),
    )
}

fn literal(value: &Value, origin: &str) -> (Value, String) {
    (
        object(vec![("kind", text("literal")), ("value", value.clone())]),
        origin.to_owned(),
    )
}

fn run_clock() -> (Value, String) {
    (
        json(r#"{"kind": "run_clock", "format": "date"}"#),
        "clock".to_owned(),
    )
}

#[test]
fn a_value_named_in_the_request_is_a_literal_even_when_it_could_be_copied() {
    let value = text("abcdefgh");
    assert_eq!(
        classify(
            &value,
            "use abcdefgh please",
            &[json(r#"{"x": "abcdefgh"}"#)]
        ),
        literal(&value, "request")
    );
}

#[test]
fn a_number_counts_only_as_a_whole_token() {
    for (value, known, expected) in [
        ("30", "A cada 30 segundos", "request"),
        ("30", "a cada 30.", "request"),
        ("30", "(30)", "request"),
        ("30", "300 vezes", "assistant"),
        ("30", "a30", "assistant"),
        ("30", "30a", "assistant"),
        ("30", "30.5", "assistant"),
        ("-123456", "saldo -123456", "request"),
        ("6", "1e-6", "assistant"),
        ("6", "1e6", "assistant"),
        ("1000", "1,000", "assistant"),
        ("2.5", "2.5 dias", "request"),
    ] {
        assert_eq!(
            classify(&json(value), known, &[]).1,
            expected,
            "{value} {known}"
        );
    }
    assert_eq!(
        span(&json("123456"), "saldo -123456", &[]).question().code,
        RERUN
    );
}

#[test]
fn booleans_null_and_empty_strings_are_never_named_or_copied() {
    for value in ["true", "false", "null", r#""""#, "[]", "{}"] {
        let value = json(value);
        let earlier = object(vec![("x", value.clone())]);
        assert_eq!(
            classify(&value, "true false null 1 0", &[earlier]),
            literal(&value, "assistant"),
            "{value:?}"
        );
    }
}

#[test]
fn the_send_date_in_the_routines_zone_is_the_run_date() {
    let day = |date: &str| object(vec![("day", text(date))]);
    assert_eq!(
        classify(&text("2026-10-05"), "relatório", &[day("2026-10-05")]),
        run_clock()
    );
    // At 23:30 in São Paulo the person's date is the run date, though it is already the next day in UTC.
    let evening = epoch(2026, 10, 7, 2, 30, 0);
    assert_eq!(
        classify_at("2026-10-06", "relatório", &[], evening, "America/Sao_Paulo"),
        run_clock()
    );
    assert_eq!(
        classify_at("2026-10-07", "relatório", &[], evening, "America/Sao_Paulo"),
        literal(&text("2026-10-07"), "assistant")
    );
    // A zone the person wrote is the Routine's zone: there, at that instant, the UTC date is the run date.
    assert_eq!(
        classify_at(
            "2026-10-07",
            "relatório em UTC",
            &[],
            evening,
            "America/Sao_Paulo"
        ),
        run_clock()
    );
}

#[test]
fn the_utc_date_of_a_send_on_another_local_date_is_a_fixed_literal() {
    let day = |date: &str| object(vec![("day", text(date))]);
    // The UTC date of a send on another local date is a fixed literal, never copied from an earlier result.
    let early = epoch(2026, 10, 5, 1, 0, 0);
    let late = epoch(2026, 10, 5, 23, 30, 0);
    for (started, timezone) in [(early, "America/Sao_Paulo"), (late, "Asia/Tokyo")] {
        let found = classify_at(
            "2026-10-05",
            "relatório",
            &[day("2026-10-05")],
            started,
            timezone,
        );
        assert_eq!(
            found,
            literal(&text("2026-10-05"), "assistant"),
            "{timezone}"
        );
    }
    assert_eq!(
        classify(
            &text("2026-10-05"),
            "desde 2026-10-05",
            &[day("2026-10-05")]
        )
        .1,
        "request"
    );
    let copied = classify(&text("2026-10-04"), "relatório", &[day("2026-10-04")]).0;
    assert_eq!(at(&copied, "kind"), &text("step_output"));
}

#[test]
fn dates_inside_text_the_assistant_wrote_follow_each_run() {
    // The owner's news Routine: its search named the day it was recorded on.
    let query = "notícias inteligência artificial hoje 5 outubro 2026";
    let search = || {
        call_value(
            "reports/fetch",
            &object(vec![("query", text(query))]),
            &json(r#"{"items": []}"#),
        )
    };
    let recorded = Span::of(vec![send(
        vec![search()],
        "Cria uma rotina que faz isso a cada 1 minuto",
    )])
    .recorded();
    let source = at(last_input(&recorded), "query");
    assert_eq!(
        (at(source, "kind"), at(source, "format")),
        (&text("run_clock"), &text("text"))
    );
    assert_eq!(recorded.origin("s1", "query"), Some("clock"));
    let zone = crate::schedule::zone_named("America/Sao_Paulo").expect("a zone");
    let tomorrow = crate::dates::clock_value(source, STARTED_AT + 86_400, &zone);
    assert_eq!(
        tomorrow,
        Some(text("notícias inteligência artificial hoje 6 outubro 2026"))
    );
}

#[test]
fn a_date_the_person_wrote_stays_and_a_result_holding_the_text_is_its_source() {
    let query = "notícias inteligência artificial hoje 5 outubro 2026";
    let search = || {
        let input = object(vec![("query", text(query))]);
        call_value("reports/fetch", &input, &json(r#"{"items": []}"#))
    };
    // The day the person wrote stays that day, and a result that holds the text is still its source.
    let named = Span::of(vec![send(
        vec![search()],
        "Notícias de 5 outubro 2026 a cada hora",
    )])
    .recorded();
    assert_eq!(
        at(at(last_input(&named), "query"), "kind"),
        &text("literal")
    );
    let beside = Span::of(vec![send(
        vec![search()],
        "A cada hora, IA de 5/10/2026 comparada com 1 a 5/11/2026",
    )])
    .recorded();
    assert_eq!(
        at(at(last_input(&beside), "query"), "kind"),
        &text("literal")
    );
    let lookup = call_value(
        "reports/fetch",
        &json(r#"{"q": "ideas"}"#),
        &object(vec![("query", text(query))]),
    );
    let sourced = Span::of(vec![super::hourly(vec![lookup, search()])]).recorded();
    assert_eq!(
        at(at(last_input(&sourced), "query"), "kind"),
        &text("step_output")
    );
}

#[test]
fn with_no_zone_known_the_run_date_is_the_utc_date() {
    let call = call_value(
        "cloudflare/list-dns-records",
        &json(r#"{"zone_id": "z", "match": "2026-10-05"}"#),
        &json("{}"),
    );
    let mut unzoned = super::hourly(vec![call]);
    unzoned.timezone = None;
    let recorded = Span::of(vec![unzoned]).recorded();
    assert_eq!(
        at(last_input(&recorded), "match"),
        &json(r#"{"kind": "run_clock", "format": "date"}"#)
    );
    assert_eq!(
        (
            at(&recorded.document, "timezone"),
            recorded.timezone_source.as_str()
        ),
        (&text("UTC"), "none")
    );
}

#[test]
fn a_value_at_one_position_is_copied_by_pointer() {
    let copied = |value: &str, earlier: &str| classify(&json(value), "x", &[json(earlier)]);
    let source = json(r#"{"kind": "step_output", "step": "s1", "pointer": "/zone/a~1b"}"#);
    assert_eq!(
        copied(r#""zone-123""#, r#"{"zone": {"a/b": "zone-123"}}"#),
        (source, "step".to_owned())
    );
    assert_eq!(
        copied(r#"{"k": 1}"#, r#"{"cfg": {"k": 1}}"#).0,
        json(r#"{"kind": "step_output", "step": "s1", "pointer": "/cfg"}"#)
    );
    assert_eq!(
        at(&copied("1234567", r#"{"n": 1234567.0}"#).0, "kind"),
        &text("step_output")
    );
    assert_eq!(
        copied("[1]", "[1]").0,
        json(r#"{"kind": "step_output", "step": "s1", "pointer": ""}"#)
    );
}

#[test]
fn short_values_are_the_assistants_and_a_value_nothing_holds_is_asked_about() {
    for (value, earlier) in [
        (r#""abcde""#, r#"{"x": "abcde"}"#),
        ("12345", r#"{"x": 12345}"#),
        ("-12345", r#"{"x": -12345}"#),
    ] {
        assert_eq!(
            classify(&json(value), "known", &[json(earlier)]),
            literal(&json(value), "assistant")
        );
    }
    assert_eq!(
        span(&text("abcdefgh"), "known", &[json(r#"{"x": "other"}"#)])
            .question()
            .code,
        RERUN
    );
}

fn targets(values: &[(&str, Option<&str>)]) -> Vec<Target> {
    values
        .iter()
        .map(|(value, label)| Target::new(text(value), *label))
        .collect()
}

#[test]
fn a_value_inside_an_array_without_a_named_member_is_asked_about() {
    let asked =
        |known: &str, earlier: &str| span(&text("id-0002"), known, &[json(earlier)]).question();
    let unnamed =
        r#"{"items": [{"id": "id-0001", "kind": "zone"}, {"id": "id-0002", "kind": "zone"}]}"#;
    let both = targets(&[("id-0001", None), ("id-0002", None)]);
    assert_eq!(
        asked("nothing named", unnamed),
        Question::new("routine-binding-ambiguous", both.clone())
    );
    // An item without the consumed member is no target.
    let missing = r#"{"items": [{"kind": "zone"}, {"id": "id-0001", "kind": "zone"}, {"id": "id-0002", "kind": "zone"}]}"#;
    assert_eq!(asked("nothing named", missing).options, both);
    assert_eq!(
        asked("x", r#"{"items": ["id-0001", "id-0002"]}"#),
        Question::new("routine-binding-ambiguous", both)
    );
    // Only one name-like member labels a target.
    let labelled = r#"{"items": [{"id": "id-0001", "name": "alpha", "title": "A"}, {"id": "id-0002", "name": "beta"}]}"#;
    assert_eq!(
        asked("x", labelled).options,
        targets(&[("id-0001", None), ("id-0002", Some("beta"))])
    );
    let nested = span(
        &text("abcdefgh"),
        "known",
        &[json(r#"{"a": [{"b": [{"id": "abcdefgh"}]}]}"#)],
    )
    .question();
    assert_eq!(
        nested,
        Question::new("routine-binding-ambiguous", targets(&[("abcdefgh", None)]))
    );
}

#[test]
fn free_text_nothing_holds_is_the_assistants_and_only_an_identifier_is_asked_about() {
    for value in [
        r#""Resumo diário do DNS""#,
        r#""two words here""#,
        r#"{"note": "x"}"#,
        r#"["a", "b"]"#,
    ] {
        assert_eq!(
            classify(&json(value), "known", &[]),
            literal(&json(value), "assistant"),
            "{value}"
        );
    }
    for value in [r#""zone-without-spaces""#, "1234567"] {
        assert_eq!(
            span(&json(value), "known", &[]).question().code,
            RERUN,
            "{value}"
        );
    }
}

#[test]
fn the_persons_answer_naming_an_unsourced_value_settles_it() {
    let work = send(vec![records()], "DNS a cada hora");
    assert_eq!(
        Span::of(vec![work.clone()]).question(),
        Question::new(RERUN, Vec::new())
    );
    let answered = Span::of(vec![work, send(Vec::new(), &format!("O id é {SHIMPZ_ID}"))])
        .asked(Asked::new(RERUN, 1))
        .recorded();
    let expected = object(vec![("kind", text("literal")), ("value", text(SHIMPZ_ID))]);
    assert_eq!(
        (
            at(last_input(&answered), "zone_id"),
            answered.origin("s1", "zone_id")
        ),
        (&expected, Some("request"))
    );
}

#[test]
fn a_value_several_sources_or_positions_hold_is_asked_about() {
    let same = Question::new("routine-binding-ambiguous", targets(&[("abcdefgh", None)]));
    let value = text("abcdefgh");
    assert_eq!(
        span(
            &value,
            "known",
            &[json(r#"{"x": "abcdefgh", "y": "abcdefgh"}"#)]
        )
        .question(),
        same
    );
    assert_eq!(
        span(
            &value,
            "known",
            &[json(r#"{"x": "abcdefgh"}"#), json(r#"{"y": "abcdefgh"}"#)]
        )
        .question(),
        same
    );
    // A container is never chosen: it refuses.
    let container = json(r#"{"cfg": {"k": 1}}"#);
    assert_eq!(
        span(&json(r#"{"k": 1}"#), "x", &[container.clone(), container]).code(),
        "routine-recording-ambiguous"
    );
}
