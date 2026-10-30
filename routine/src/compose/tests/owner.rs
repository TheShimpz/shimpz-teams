//! The owner's own recordings: a zone selected by the name the person gave, targets the person chose, and the texts
//! that name a value.

use strict_json::Value;

use super::PIN;
use super::{
    SELECTED, SHIMPZ_ID, Span, TWIN_ID, actions, asked_after, binding_question, call, call_value,
    input_of, last_input, owner, records, send, then_hourly, twins, zone_id, zones, zones_call,
};
use crate::json::{as_str, at, elements, json_text, text};
use crate::plan::VERSION;
use crate::recording::{Asked, Question, RERUN, Target};
use crate::testing::json;

fn literal(value: &str) -> Value {
    json(&format!(r#"{{"kind": "literal", "value": "{value}"}}"#))
}

fn chosen(id: &str) -> crate::recording::Send {
    send(Vec::new(), &json_text(&text(id)))
}

fn twins_call() -> crate::trace::Occurrence {
    call_value("cloudflare/list-zones", &json("{}"), &twins())
}

fn records_of(zone: &str) -> crate::trace::Occurrence {
    call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{zone}"}}"#),
        r#"{"result": []}"#,
    )
}

#[test]
fn the_zone_id_is_selected_by_the_zone_name_the_person_gave() {
    let recorded = Span::of(vec![send(vec![zones_call(), records()], &owner())]).recorded();
    let expected = json(&format!(
        r#"{{"version": {VERSION}, "timezone": "America/Sao_Paulo",
            "steps": [{{"id": "s1", "assistant": "cloudflare", "action": "list-zones", "pin": "{PIN}", "input": {{}}}},
                      {{"id": "s2", "assistant": "cloudflare", "action": "list-dns-records", "pin": "{PIN}",
                        "input": {{"zone_id": {SELECTED}}}}}],
            "output": {{"mode": "show", "step": "s2"}}}}"#
    ));
    assert_eq!(recorded.document, expected);
    assert_eq!(
        recorded.origins,
        [
            ("s1".to_owned(), vec![]),
            (
                "s2".to_owned(),
                vec![("zone_id".to_owned(), "selector".to_owned())]
            )
        ]
    );
    assert_eq!(
        recorded.schedule,
        json(r#"{"kind": "continuous", "gap": 30}"#)
    );
    assert_eq!(recorded.timezone_source, "browser");
    let permitted: Vec<&str> = recorded
        .permitted
        .iter()
        .filter_map(|item| as_str(at(item, "action")))
        .collect();
    assert_eq!(permitted, ["list-dns-records", "list-zones"]);
}

#[test]
fn the_zone_looked_up_in_an_earlier_send_is_still_the_source() {
    let recorded = Span::of(vec![
        send(vec![zones_call()], "Cria uma rotina pra mim"),
        send(Vec::new(), "Listar registros DNS"),
        send(vec![records()], "shimpz.com, a cada 30 segundos"),
    ])
    .recorded();
    assert_eq!(actions(&recorded), ["list-zones", "list-dns-records"]);
    assert_eq!(last_input(&recorded).get("zone_id"), Some(&json(SELECTED)));
}

#[test]
fn a_lookup_after_its_use_is_still_its_source() {
    // Primed: the zones were listed earlier and again after the records; unprimed: only after.
    let primed = Span::of(vec![
        send(vec![zones_call()], "Liste os registros DNS de shimpz.com"),
        send(vec![records(), zones_call()], "Faça isso a cada hora"),
    ])
    .recorded();
    let unprimed = Span::of(vec![send(
        vec![records(), zones_call()],
        "DNS de shimpz.com a cada hora",
    )])
    .recorded();
    for recorded in [primed, unprimed] {
        assert_eq!(actions(&recorded), ["list-zones", "list-dns-records"]);
        assert_eq!(last_input(&recorded).get("zone_id"), Some(&json(SELECTED)));
        assert_eq!(at(at(&recorded.document, "output"), "step"), &text("s2"));
    }
}

#[test]
fn a_remembered_id_no_result_holds_is_asked_about() {
    let question = Span::of(vec![send(vec![records()], "shimpz.com a cada hora")]).question();
    assert_eq!(question, Question::new(RERUN, Vec::new()));
}

#[test]
fn two_zones_of_the_named_name_are_asked_about_with_both_targets() {
    let work = send(
        vec![twins_call(), records()],
        "DNS de shimpz.com a cada hora",
    );
    let question = Span::of(vec![work.clone()]).question();
    let targets = vec![
        Target::new(text(SHIMPZ_ID), Some("shimpz.com")),
        Target::new(text(TWIN_ID), Some("shimpz.com")),
    ];
    assert_eq!(question, binding_question(targets));
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    // Choosing the zone the work used, by its exact JSON text, makes it a literal the person named.
    let picked = Span::of(vec![work.clone(), chosen(SHIMPZ_ID)])
        .asked(pending.clone())
        .recorded();
    assert_eq!(
        last_input(&picked).get("zone_id"),
        Some(&literal(SHIMPZ_ID))
    );
    assert_eq!(picked.origin("s2", "zone_id"), Some("request"));
    // Naming the other one asks for the work again, with that zone.
    let rerun = Span::of(vec![work, chosen(TWIN_ID)])
        .asked(pending)
        .question();
    assert_eq!(rerun, Question::new(RERUN, Vec::new()));
}

fn same_named_work() -> crate::recording::Send {
    let items =
        r#"{"items": [{"name": "same", "id": "target"}, {"name": "same", "id": "target-b"}]}"#;
    send(
        vec![
            call("reports/fetch", "{}", items),
            call(
                "cloudflare/list-dns-records",
                r#"{"zone_id": "target"}"#,
                "{}",
            ),
        ],
        &then_hourly("same"),
    )
}

#[test]
fn only_an_exact_answer_confirms_a_target_and_a_substring_never_does() {
    let work = same_named_work();
    let question = Span::of(vec![work.clone()]).question();
    assert_eq!(question.code, "routine-binding-ambiguous");
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    // Naming the other target asks for the work again, though the work's own target is a substring of the answer.
    let answered = |answer: &str| {
        Span::of(vec![work.clone(), send(Vec::new(), answer)]).asked(pending.clone())
    };
    assert_eq!(answered(r#""target-b""#).question().code, RERUN);
    // An answer that is no target's exact JSON text confirms none: the question stands.
    for answer in ["quero o target-b", "target", "target-b"] {
        assert_eq!(
            answered(answer).question().code,
            "routine-binding-ambiguous",
            "{answer}"
        );
    }
    let recorded = answered(r#""target""#).recorded();
    assert_eq!(at(last_input(&recorded), "zone_id"), &literal("target"));
}

#[test]
fn a_string_and_an_integer_target_with_the_same_digits_stay_distinct() {
    let digits = "12345678901234567890";
    let items = format!(
        r#"{{"items": [{{"name": "same", "id": "{digits}"}}, {{"name": "same", "id": {digits}}}]}}"#
    );
    let work = send(
        vec![
            call("reports/fetch", "{}", &items),
            call(
                "cloudflare/list-dns-records",
                &format!(r#"{{"zone_id": "{digits}"}}"#),
                "{}",
            ),
        ],
        &then_hourly("same"),
    );
    let question = Span::of(vec![work.clone()]).question();
    let wire = question.wire();
    let values: Vec<&Value> = elements(at(&wire, "options"))
        .iter()
        .map(|option| at(option, "value"))
        .collect();
    assert_eq!(values, [&text(&json_text(&text(digits))), &text(digits)]);
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    let answered = |answer: &str| {
        Span::of(vec![work.clone(), send(Vec::new(), answer)]).asked(pending.clone())
    };
    assert_eq!(answered(digits).question().code, RERUN);
    assert_eq!(
        at(
            last_input(&answered(&json_text(&text(digits))).recorded()),
            "zone_id"
        ),
        &literal(digits)
    );
}

#[test]
fn one_answer_binds_every_occurrence_of_its_target_and_never_an_independent_one() {
    let other = records_of(&zone_id(1));
    // Mixed: shimpz.com is ambiguous, other.org resolves by its own name.
    let mixed = send(
        vec![twins_call(), records(), other],
        "DNS de shimpz.com e other.org a cada hora",
    );
    let question = Span::of(vec![mixed.clone()]).question();
    assert_eq!(question.code, "routine-binding-ambiguous");
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    let answered = Span::of(vec![mixed, chosen(SHIMPZ_ID)])
        .asked(pending)
        .recorded();
    assert_eq!(at(input_of(&answered, 1), "zone_id"), &literal(SHIMPZ_ID));
    assert_eq!(
        at(at(input_of(&answered, 2), "zone_id"), "where"),
        &json(r#"{"name": "other.org"}"#)
    );
    // Twin-only: both twins were used, so one answer binds both, and the other twin's call was the choice not taken.
    let both = send(
        vec![twins_call(), records(), records_of(TWIN_ID)],
        "DNS de shimpz.com a cada hora",
    );
    let question = Span::of(vec![both.clone()]).question();
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    let kept = Span::of(vec![both, chosen(SHIMPZ_ID)])
        .asked(pending)
        .recorded();
    assert_eq!(elements(at(&kept.document, "steps")).len(), 2);
    assert_eq!(at(input_of(&kept, 1), "zone_id"), &literal(SHIMPZ_ID));
}

#[test]
fn a_choice_among_twins_the_work_both_read_keeps_the_chosen_call_and_runs_nothing_again() {
    // From a live trace: the agent read the records of both zones named shimpz.com, the person chose one.
    let message = "A cada hora, liste os registros DNS de shimpz.com";
    let work = send(vec![twins_call(), records(), records_of(TWIN_ID)], message);
    let question = Span::of(vec![work.clone()]).question();
    assert_eq!(question.code, "routine-binding-ambiguous");
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    let recorded = Span::of(vec![work, chosen(SHIMPZ_ID)])
        .asked(pending.clone())
        .recorded();
    assert_eq!(actions(&recorded), ["list-zones", "list-dns-records"]);
    assert_eq!(
        (
            at(last_input(&recorded), "zone_id"),
            recorded.origin("s2", "zone_id")
        ),
        (&literal(SHIMPZ_ID), Some("request"))
    );
    assert_eq!(at(at(&recorded.document, "output"), "step"), &text("s2"));
    // Choosing the twin works the same way, in either order of the calls.
    let reordered = send(vec![twins_call(), records_of(TWIN_ID), records()], message);
    let other = Span::of(vec![reordered, chosen(TWIN_ID)])
        .asked(pending)
        .recorded();
    assert_eq!(at(last_input(&other), "zone_id"), &literal(TWIN_ID));
}

#[test]
fn a_changing_call_on_a_target_the_person_did_not_choose_is_never_dropped() {
    let deletes: Vec<crate::trace::Occurrence> = [SHIMPZ_ID, TWIN_ID]
        .iter()
        .map(|zone| {
            let input = format!(
                r#"{{"zone_id": "{zone}", "record_id": "{}"}}"#,
                "r".repeat(32)
            );
            call("cloudflare/delete-dns-record", &input, "{}")
        })
        .collect();
    let mut calls = vec![twins_call()];
    calls.extend(deletes);
    let work = send(calls, "A cada hora, apague o registro de shimpz.com");
    let question = Span::of(vec![work.clone()]).question();
    assert_eq!(question.code, "routine-binding-ambiguous");
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    let rerun = Span::of(vec![work, chosen(SHIMPZ_ID)])
        .asked(pending)
        .question();
    assert_eq!(rerun.code, RERUN);
    assert_eq!(rerun.manifest.map(|manifest| manifest.slots.len()), Some(3));
}

/// Work over two names that each have a twin, both choices answered: the span and what it settled.
struct TwoChoices {
    listing: crate::trace::Occurrence,
    answers: Vec<crate::recording::Send>,
    second: Question,
    used: String,
    other_id: String,
    other_twin: String,
}

fn group_of(question: &Question, other_id: &str) -> String {
    let offers_other = question
        .pending
        .iter()
        .flat_map(|pending| &pending.targets)
        .any(|target| as_str(&target.value) == Some(other_id));
    if offers_other {
        other_id.to_owned()
    } else {
        SHIMPZ_ID.to_owned()
    }
}

fn two_choices() -> TwoChoices {
    let (other_id, other_twin) = (zone_id(1), "8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5".to_owned());
    let mut items = elements(at(&twins(), "result")).to_vec();
    items.push(json(&format!(
        r#"{{"id": "{other_twin}", "name": "other.org", "created_on": "2026-01-06"}}"#
    )));
    let result = Value::Object(vec![("result".to_owned(), Value::Array(items))]);
    let listing = call_value("cloudflare/list-zones", &json("{}"), &result);
    let message = "DNS de other.org e shimpz.com a cada hora";
    let work = send(
        vec![listing.clone(), records_of(&other_id), records()],
        message,
    );
    let first = Span::of(vec![work.clone()]).question();
    let used = group_of(&first, &other_id);
    let answer = chosen(&used);
    let second = Span::of(vec![work.clone(), answer.clone()])
        .asked(asked_after(&first, 1))
        .question();
    assert_eq!(second.code, "routine-binding-ambiguous");
    let remaining = group_of(&second, &other_id);
    assert_ne!(remaining, used);
    let answers = vec![work, answer, chosen(&remaining)];
    TwoChoices {
        listing,
        answers,
        second,
        used,
        other_id,
        other_twin,
    }
}

fn zone_values(recorded: &crate::recording::Recorded) -> std::collections::BTreeSet<String> {
    (1..3)
        .filter_map(|step| {
            as_str(at(at(input_of(recorded, step), "zone_id"), "value")).map(str::to_owned)
        })
        .collect()
}

#[test]
fn every_answered_binding_stays_bound_through_later_binding_questions() {
    let span = two_choices();
    let recorded = Span::of(span.answers.clone())
        .asked(asked_after(&span.second, 2))
        .recorded();
    let expected = std::collections::BTreeSet::from([span.other_id.clone(), SHIMPZ_ID.to_owned()]);
    assert_eq!(zone_values(&recorded), expected);
}

#[test]
fn work_that_uses_only_an_unchosen_twin_runs_again_and_beside_the_chosen_one_is_dropped() {
    let span = two_choices();
    let rejected_zone = if span.used == SHIMPZ_ID {
        TWIN_ID.to_owned()
    } else {
        span.other_twin.clone()
    };
    let rejected = call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{rejected_zone}"}}"#),
        r#"{"n": 1}"#,
    );
    // Work that later uses a twin the person did not choose, and never the one they chose, is run again.
    let mut later = span.answers.clone();
    later.push(send(
        vec![span.listing.clone(), rejected.clone()],
        super::EVERY_HOUR,
    ));
    assert_eq!(
        Span::of(later)
            .asked(asked_after(&span.second, 2))
            .question()
            .code,
        RERUN
    );
    // Beside the chosen one, its call was the choice not taken.
    let mut beside = span.answers.clone();
    let calls = vec![
        span.listing.clone(),
        records_of(&span.other_id),
        rejected,
        records(),
    ];
    beside.push(send(calls, super::EVERY_HOUR));
    let recorded = Span::of(beside)
        .asked(asked_after(&span.second, 2))
        .recorded();
    let expected = std::collections::BTreeSet::from([span.other_id.clone(), SHIMPZ_ID.to_owned()]);
    assert_eq!(zone_values(&recorded), expected);
}

#[test]
fn work_run_again_for_a_chosen_target_must_use_exactly_that_target() {
    let items =
        r#"{"items": [{"name": "same", "id": "target"}, {"name": "same", "id": "target-b"}]}"#;
    let fetch = call("reports/fetch", "{}", items);
    let reading = |zone: &str| {
        call(
            "cloudflare/list-dns-records",
            &format!(r#"{{"zone_id": "{zone}"}}"#),
            "{}",
        )
    };
    let work = send(vec![fetch.clone(), reading("target")], &then_hourly("same"));
    let question = Span::of(vec![work.clone()]).question();
    let answer = send(Vec::new(), r#""target-b""#);
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    let rerun = Span::of(vec![work.clone(), answer.clone()])
        .asked(pending)
        .question();
    let again = send(vec![fetch.clone(), reading("target-b")], super::EVERY_HOUR);
    let recorded = Span::of(vec![work.clone(), answer.clone(), again])
        .asked(asked_after(&rerun, 2))
        .recorded();
    assert_eq!(
        (
            at(last_input(&recorded), "zone_id"),
            recorded.origin("s2", "zone_id")
        ),
        (&literal("target-b"), Some("request"))
    );
    // Work run again for the other target contradicts the choice: the span ends, never asks again.
    let wrong = send(vec![fetch.clone(), reading("target")], super::EVERY_HOUR);
    let span = Span::of(vec![work.clone(), answer.clone(), wrong]).asked(asked_after(&rerun, 2));
    assert_eq!(span.code(), "routine-record-again");
    // A read for the unchosen target beside the chosen one is the choice not taken, which the work drops.
    let both = send(
        vec![fetch, reading("target-b"), reading("target")],
        super::EVERY_HOUR,
    );
    let recorded = Span::of(vec![work, answer, both])
        .asked(asked_after(&rerun, 2))
        .recorded();
    assert_eq!(at(last_input(&recorded), "zone_id"), &literal("target-b"));
}

#[test]
fn a_zone_listed_without_a_name_is_never_read_by_its_position() {
    // A reordered list would make a position target another zone, so the person is asked which one it is.
    let first = elements(at(&zones(), "result"))[0].clone();
    let first_id = as_str(at(&first, "id")).expect("an id").to_owned();
    let first_name = as_str(at(&first, "name")).expect("a name").to_owned();
    let work = send(
        vec![zones_call(), records_of(&first_id)],
        "Liste os DNS a cada hora",
    );
    let question = Span::of(vec![work.clone()]).question();
    let targets: Vec<Target> = elements(at(&zones(), "result"))
        .iter()
        .map(|item| Target::new(at(item, "id").clone(), as_str(at(item, "name"))))
        .collect();
    assert_eq!(question, binding_question(targets));
    let pending = Asked {
        pending: question.pending.clone(),
        ..Asked::new(&question.code, 1)
    };
    // Naming the zone's name selects its item by that name; choosing its id fixes the id the person chose.
    let by_name = Span::of(vec![work.clone(), send(Vec::new(), &first_name)])
        .asked(pending.clone())
        .recorded();
    let selector = format!(
        r#"{{"kind": "step_output", "step": "s1", "pointer": "/result", "where": {{"name": "{first_name}"}}, "item": "/id"}}"#
    );
    assert_eq!(at(last_input(&by_name), "zone_id"), &json(&selector));
    let by_id = Span::of(vec![work.clone(), chosen(&first_id)])
        .asked(pending.clone())
        .recorded();
    assert_eq!(by_id.origin("s2", "zone_id"), Some("request"));
    let other = Span::of(vec![work, chosen(&zone_id(1))])
        .asked(pending)
        .question();
    assert_eq!(other, Question::new(RERUN, Vec::new()));
}

#[test]
fn a_name_from_an_earlier_send_of_the_conversation_anchors_the_selector() {
    let records = call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{SHIMPZ_ID}", "per_page": 50}}"#),
        r#"{"result": []}"#,
    );
    let mut windowed = send(vec![zones_call(), records], "Faça isso a cada hora");
    windowed.window = vec!["Liste os registros DNS de shimpz.com, 50 por página".to_owned()];
    let recorded = Span::of(vec![windowed]).recorded();
    assert_eq!(
        at(at(last_input(&recorded), "zone_id"), "where"),
        &json(r#"{"name": "shimpz.com"}"#)
    );
    assert_eq!(recorded.origin("s2", "zone_id"), Some("selector"));
    assert_eq!(recorded.origin("s2", "per_page"), Some("request"));
}

#[test]
fn no_name_or_number_spans_two_lines_and_a_question_line_names_nothing() {
    let fetch = call("reports/fetch", r#"{"site": "ab.cd", "port": 8443}"#, "{}");
    let recorded = Span::of(vec![send(
        vec![fetch],
        &format!("ab.\ncd 84\n43 hoje\n{}", super::EVERY_HOUR),
    )])
    .recorded();
    assert_eq!(
        (recorded.origin("s1", "site"), recorded.origin("s1", "port")),
        (Some("assistant"), Some("assistant"))
    );
    let composed = team_protocol::payload::compose_clarified(
        "Liste os registros",
        "Da zona shimpz-zone-1?",
        "sim, a cada hora",
        "pt",
    )
    .expect("composed");
    let site = call("reports/fetch", r#"{"site": "shimpz-zone-1"}"#, "{}");
    assert_eq!(
        Span::of(vec![send(vec![site], &composed)]).question().code,
        RERUN
    );
}
