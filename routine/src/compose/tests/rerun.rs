//! `test_routine_rerun.py`: the work a question asks the Brain to run again, and the one repair that work gets.

use strict_json::Value;

use super::{
    SELECTED, SHIMPZ_ID, Span, actions, asked_after, call, call_value, call_with_input, hourly,
    last_input, pending_after, post, records, send, zone_id, zones_call,
};
use crate::json::{at, elements, int, json_text, object, text};
use crate::provenance::sent_value;
use crate::recording::{Asked, Call, RERUN, Slot};
use crate::testing::json;
use crate::trace::Kept;

fn chosen_answer(id: &str) -> crate::recording::Send {
    send(Vec::new(), &json_text(&text(id)))
}

fn reading(zone: &str) -> crate::trace::Occurrence {
    call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{zone}"}}"#),
        r#"{"result": []}"#,
    )
}

#[test]
fn an_unsourced_value_stands_until_work_runs_and_then_gets_one_repair() {
    let first = send(vec![records()], "DNS de shimpz.com a cada hora");
    let asked = Span::of(vec![first.clone()]).question();
    assert_eq!(asked.code, RERUN);
    let pending = pending_after(&asked, 1);
    // A send that runs no Action leaves the question standing.
    let standing = Span::of(vec![first.clone(), send(Vec::new(), "ok")])
        .asked(pending.clone())
        .question();
    assert_eq!(standing.code, asked.code);
    // Work that remembers the value again ends the span instead of asking once more.
    for later in [hourly(vec![records()]), hourly(vec![records(), records()])] {
        assert_eq!(
            Span::of(vec![first.clone(), later])
                .asked(pending.clone())
                .code(),
            "routine-record-again"
        );
    }
    let recorded = Span::of(vec![first, hourly(vec![zones_call(), records()])])
        .asked(pending)
        .recorded();
    assert_eq!(
        (actions(&recorded), at(last_input(&recorded), "zone_id")),
        (vec!["list-zones", "list-dns-records"], &json(SELECTED))
    );
}

#[test]
fn work_rerun_with_other_words_records_the_latest_work_and_never_asks_about_a_split() {
    // The owner's football Routine: each answer reran the search with other words.
    let search = |query: &str| {
        let input = object(vec![("query", text(query)), ("num_results", int(5))]);
        call_value(
            "reports/fetch",
            &input,
            &json(&format!(r#"{{"results": [{{"title": "{query}"}}]}}"#)),
        )
    };
    let recorded = Span::of(vec![
        send(
            vec![search("jogos de futebol da semana")],
            "Pesquise os jogos de futebol da semana",
        ),
        send(
            vec![search("principais jogos de futebol esta semana")],
            "Principais jogos",
        ),
        send(
            vec![search("jogos futebol brasileirão e europa semana")],
            "a cada 30 dias",
        ),
    ])
    .recorded();
    assert_eq!(actions(&recorded), ["fetch"]);
    assert_eq!(
        at(at(last_input(&recorded), "query"), "value"),
        &text("jogos futebol brasileirão e europa semana")
    );
    assert_eq!(
        recorded.schedule,
        json(r#"{"kind": "continuous", "gap": 2592000}"#)
    );
    // Earlier changes are never split work: the latest send is the work.
    let posts = Span::of(vec![
        send(vec![post("a")], "a cada hora"),
        hourly(vec![post("b"), post("b")]),
    ])
    .recorded();
    let texts: Vec<&Value> = elements(at(&posts.document, "steps"))
        .iter()
        .map(|step| at(at(at(step, "input"), "t"), "value"))
        .collect();
    assert_eq!(texts, [&text("b"), &text("b")]);
}

/// Records for two zones, then the person chose a third, shimpz.com: the first send, the answer, and the rerun.
fn chose_third(
    message: &str,
) -> (
    crate::recording::Send,
    crate::recording::Send,
    crate::recording::Question,
) {
    let first = send(
        vec![zones_call(), reading(&zone_id(0)), reading(&zone_id(1))],
        message,
    );
    let asked = Span::of(vec![first.clone()]).question();
    assert_eq!(asked.code, "routine-binding-ambiguous");
    let answer = chosen_answer(SHIMPZ_ID);
    let rerun = Span::of(vec![first.clone(), answer.clone()])
        .asked(asked_after(&asked, 1))
        .question();
    assert_eq!(rerun.code, RERUN);
    (first, answer, rerun)
}

#[test]
fn a_chosen_target_must_be_what_the_repair_sends() {
    let (first, answer, rerun) = chose_third("A cada 5 segundos, liste minhas zonas");
    // Work run again for another zone, or still for the unchosen ones, ends the span.
    for later in [
        hourly(vec![zones_call(), reading(&zone_id(0))]),
        hourly(vec![
            zones_call(),
            reading(&zone_id(0)),
            reading(&zone_id(1)),
        ]),
    ] {
        let span =
            Span::of(vec![first.clone(), answer.clone(), later]).asked(asked_after(&rerun, 2));
        assert_eq!(span.code(), "routine-record-again");
    }
    let recorded = Span::of(vec![
        first,
        answer,
        hourly(vec![zones_call(), reading(SHIMPZ_ID)]),
    ])
    .asked(asked_after(&rerun, 2))
    .recorded();
    assert_eq!(actions(&recorded), ["list-zones", "list-dns-records"]);
    assert_eq!(
        at(at(last_input(&recorded), "zone_id"), "value"),
        &text(SHIMPZ_ID)
    );
}

#[test]
fn a_call_that_sends_nothing_or_a_withheld_value_never_sends_a_chosen_target() {
    let withheld = Kept {
        value: json(r#"{"text": null}"#),
        withheld: ["/text".to_owned()].into(),
        oversize: false,
    };
    for occurrence in [
        call("reports/post", "{}", "{}"),
        call_with_input("reports/post", withheld, "{}"),
    ] {
        assert_eq!(
            sent_value(
                &Call {
                    index: 0,
                    send: 0,
                    occurrence
                },
                "text"
            ),
            None
        );
    }
}

#[test]
fn a_repair_that_drops_the_chosen_target_ends_the_span_whatever_is_asked_between() {
    let (first, answer, rerun) = chose_third("A cada 5 segundos, liste minhas zonas");
    // Neither a listing without a zone nor a zone another lookup returned is the zone the person chose.
    let lookup = call(
        "reports/fetch",
        r#"{"q": "zone"}"#,
        r#"{"id": "zone-from-lookup"}"#,
    );
    let other = hourly(vec![
        lookup,
        call(
            "cloudflare/list-dns-records",
            r#"{"zone_id": "zone-from-lookup"}"#,
            "{}",
        ),
    ]);
    for later in [
        hourly(vec![
            zones_call(),
            call(
                "cloudflare/list-dns-records",
                r#"{"zone_id": "z", "per_page": 5}"#,
                "{}",
            ),
        ]),
        other.clone(),
    ] {
        let span =
            Span::of(vec![first.clone(), answer.clone(), later]).asked(asked_after(&rerun, 2));
        assert_eq!(span.code(), "routine-record-again");
    }
    let (first, answer, rerun) = chose_third("Liste minhas zonas");
    let asked = Asked {
        chosen: rerun.chosen.clone(),
        repair_from: rerun.repair_from,
        ..Asked::new("routine-schedule-unstated", 3)
    };
    let span = Span::of(vec![first, answer, other, send(Vec::new(), "a cada hora")]).asked(asked);
    assert_eq!(span.code(), "routine-record-again");
}

#[test]
fn a_target_question_left_open_does_not_hold_back_other_work() {
    let twins =
        r#"{"result": [{"id": "zone-aaaa-1", "name": "a"}, {"id": "zone-bbbb-2", "name": "a"}]}"#;
    let first = send(
        vec![
            call("cloudflare/list-zones", "{}", twins),
            reading("zone-aaaa-1"),
        ],
        "DNS de a a cada hora",
    );
    let asked = Span::of(vec![first.clone()]).question();
    assert_eq!(asked.code, "routine-binding-ambiguous");
    // The person typed no target, and the agent ran other work: the question it left open binds nothing.
    let recorded = Span::of(vec![
        first,
        send(vec![zones_call()], "na verdade, só as zonas"),
    ])
    .asked(asked_after(&asked, 1))
    .recorded();
    assert_eq!(actions(&recorded), ["list-zones"]);
}

#[test]
fn an_earlier_dependency_of_the_work_keeps_its_own_ambiguity() {
    let lookup = call(
        "reports/fetch",
        r#"{"q": "zones"}"#,
        r#"{"zone": "zone-identifier-1"}"#,
    );
    let report = call(
        "reports/fetch",
        r#"{"zone": "zone-identifier-1"}"#,
        r#"{"report": "report-identifier-1"}"#,
    );
    let read = call(
        "reports/fetch",
        r#"{"report": "report-identifier-1"}"#,
        r#"{"meta": {"zone": "zone-identifier-1"}}"#,
    );
    let asked = Span::of(vec![
        send(vec![lookup, report], "relatório"),
        send(vec![read], "a cada hora"),
    ])
    .question();
    assert_eq!(asked.code, "routine-binding-ambiguous");
}

#[test]
fn a_withheld_or_oversize_call_beside_a_rerun_question_refuses_and_is_never_frozen() {
    let hidden = || Kept {
        value: json(r#"{"t": null}"#),
        withheld: ["/t".to_owned()].into(),
        oversize: false,
    };
    let message = "DNS de shimpz.com a cada hora";
    assert_eq!(
        Span::of(vec![send(
            vec![call_with_input("reports/post", hidden(), "{}"), records()],
            message
        )])
        .code(),
        "routine-secret-literal"
    );
    let oversize = Kept {
        value: Value::Null,
        withheld: [String::new()].into(),
        oversize: true,
    };
    assert_eq!(
        Span::of(vec![send(
            vec![call_with_input("reports/post", oversize, "{}"), records()],
            message
        )])
        .code(),
        "routine-recording-too-large"
    );
    for action in ["reports/post", "reports/fetch"] {
        assert_eq!(
            Span::of(vec![send(
                vec![call_with_input(action, hidden(), "{}")],
                "a cada hora"
            )])
            .code(),
            "routine-secret-literal"
        );
    }
}

fn slot_inputs(span: &Span) -> Vec<(String, String, Value)> {
    span.question().manifest.expect("a manifest").slots[0]
        .inputs
        .clone()
}

fn input(member: &str, kind: &str, value: Value) -> (String, String, Value) {
    (member.to_owned(), kind.to_owned(), value)
}

#[test]
fn each_remembered_value_is_its_own_slot_and_read_only_twins_are_one() {
    let other = reading(&zone_id(1));
    let asked = Span::of(vec![send(
        vec![records(), other],
        "DNS de shimpz.com e other.org a cada hora",
    )])
    .question();
    let slots = asked.manifest.expect("a manifest").slots;
    assert_eq!(slots.len(), 2);
    let action = ("cloudflare".to_owned(), "list-dns-records".to_owned());
    assert_eq!(
        slots[0],
        Slot {
            action,
            inputs: vec![input("zone_id", "fresh", text(SHIMPZ_ID))],
            sources: Vec::new()
        }
    );
    let remembered = call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{}"}}"#, "f".repeat(32)),
        "{}",
    );
    let asked = Span::of(vec![send(
        vec![zones_call(), zones_call(), remembered],
        "DNS a cada hora",
    )])
    .question();
    assert_eq!(asked.code, RERUN);
    let named: Vec<String> = asked
        .manifest
        .expect("a manifest")
        .slots
        .iter()
        .map(|slot| slot.action.1.clone())
        .collect();
    assert_eq!(named, ["list-zones", "list-dns-records"]);
}

#[test]
fn free_text_and_dates_are_named_as_the_recording_keeps_them() {
    let typed = "some freely typed text";
    let remembered = call_value(
        "cloudflare/list-dns-records",
        &object(vec![("zone_id", text(SHIMPZ_ID)), ("query", text(typed))]),
        &json(r#"{"result": []}"#),
    );
    let inputs = slot_inputs(&Span::of(vec![send(
        vec![remembered],
        "DNS de shimpz.com a cada hora",
    )]));
    assert_eq!(
        inputs,
        [
            input("query", "value", text(typed)),
            input("zone_id", "fresh", text(SHIMPZ_ID))
        ]
    );
    let dated = || {
        call(
            "reports/fetch",
            r#"{"day": "2026-10-05", "id": "remembered-1"}"#,
            "{}",
        )
    };
    let run_date = slot_inputs(&Span::of(vec![send(
        vec![dated()],
        "relatório a cada hora",
    )]));
    assert_eq!(
        run_date,
        [
            input("day", "clock", Value::Null),
            input("id", "fresh", text("remembered-1"))
        ]
    );
    let named = slot_inputs(&Span::of(vec![send(
        vec![dated()],
        "Relatório de 2026-10-05 a cada hora",
    )]));
    assert_eq!(
        named,
        [
            input("day", "value", text("2026-10-05")),
            input("id", "fresh", text("remembered-1"))
        ]
    );
    // At 01:00 UTC it is still the day before in São Paulo, so the recorder keeps the UTC date as a literal.
    let mut early = send(vec![dated()], "relatório a cada hora");
    early.started_at = crate::testing::epoch(2026, 10, 5, 1, 0, 0);
    assert_eq!(slot_inputs(&Span::of(vec![early])), named);
}

#[test]
fn a_one_word_dated_text_is_named_as_the_recorder_keeps_it() {
    // The recorder writes its date for each run; work run again sends it as it is, never a value to look up.
    let dated = call(
        "reports/fetch",
        r#"{"name": "relatorio_2026-10-05", "id": "remembered-1"}"#,
        "{}",
    );
    let inputs = slot_inputs(&Span::of(vec![send(vec![dated], "relatório a cada hora")]));
    assert_eq!(
        inputs,
        [
            input("id", "fresh", text("remembered-1")),
            input("name", "value", text("relatorio_2026-10-05"))
        ]
    );
    let recorded = Span::of(vec![hourly(vec![call(
        "reports/post",
        r#"{"text": "relatorio_2026-10-05"}"#,
        "{}",
    )])])
    .recorded();
    assert_eq!(
        (
            at(at(last_input(&recorded), "text"), "kind"),
            recorded.origin("s1", "text")
        ),
        (&text("run_clock"), Some("clock"))
    );
}
