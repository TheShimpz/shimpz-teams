//! The memory-only recording span (`tests/test_local_routine_recorder.py`, `RecordingBookTests`).

use std::sync::Arc;
use std::time::{Duration, SystemTime};

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::testing::FakeClock;
use team_inference::runtime::ConversationEntry;
use team_inference::usage::Spent;
use team_protocol::payload::compose_clarified;
use team_routine::recording::{Manifest, Pending, Question, Slot, Target};
use team_routine::reference::Referred;
use team_routine::trace::{self, Occurrence, Protection};

use super::*;

const PRINCIPAL: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const INCARNATION: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const BINDING: (&str, &str) = (PRINCIPAL, INCARNATION);

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn book_at(clock: &Arc<FakeClock>) -> RecordingBook {
    RecordingBook::new(clock.clone(), Arc::new(OsEntropy))
}

fn book() -> RecordingBook {
    book_at(&Arc::new(FakeClock::at(SystemTime::UNIX_EPOCH)))
}

fn started(message: &str) -> Started {
    Started {
        message: message.to_owned(),
        conversation: Vec::new(),
        timezone: None,
        locale: None,
    }
}

fn start(book: &RecordingBook, team: &str, message: &str, now: i64) -> String {
    book.start(team, BINDING, &started(message), now)
        .expect("started")
}

fn occurrence(size: usize) -> Occurrence {
    let empty = json("{}");
    let input = trace::keep(&empty, &empty, &[]).expect("kept");
    let result = trace::keep(&Value::String("x".repeat(size)), &empty, &[]).expect("kept");
    Occurrence::new(
        "shimpz-cloudflare",
        "list-zones",
        &format!("sha256:{}", "a".repeat(64)),
        true,
        input,
        result,
    )
}

fn intent(work: Option<&str>) -> Intent {
    Intent {
        name: "DNS".to_owned(),
        replaces: None,
        revision: None,
        description: "Lista o DNS.".to_owned(),
        work: work.map(str::to_owned),
    }
}

fn answer(original: &str, text: &str) -> String {
    compose_clarified(original, "Qual?", text, "pt").expect("composed")
}

#[test]
fn a_span_is_reachable_only_by_its_latest_send_and_a_persons_next_send_continues_it() {
    let book = book();
    let first = start(&book, "team_1", "message", 1);
    assert!(book.get("team_1", None).is_none());
    assert!(book.get("team_1", Some(&"f".repeat(32))).is_none());
    assert!(book.get("team_2", Some(&first)).is_none());
    book.protect("team_1", &"f".repeat(32), &["other".to_owned()]);
    book.protect("team_1", &first, &["kept".to_owned()]);
    let second = start(&book, "team_1", "again", 2);
    assert!(book.get("team_1", Some(&first)).is_none());
    let found = book.get("team_1", Some(&second)).expect("span");
    let messages: Vec<&str> = found
        .sends
        .iter()
        .map(|send| send.message.as_str())
        .collect();
    assert_eq!(messages, ["message", "again"]);
    assert_eq!(found.protection.values.iter().collect::<Vec<_>>(), ["kept"]);
    assert_eq!(
        (found.recording_id(), found.started_at()),
        (second.as_str(), 2)
    );
    book.finish("team_2", &second);
    book.finish("team_1", &first);
    assert!(book.get("team_1", Some(&second)).is_some());
    book.finish("team_1", &second);
    assert!(book.get("team_1", Some(&second)).is_none());
}

#[test]
fn another_person_another_incarnation_a_stale_or_refused_span_starts_anew() {
    let book = book();
    start(&book, "team_1", "m", 1);
    let other = "c".repeat(32);
    let elsewhere = "d".repeat(64);
    for (binding, now) in [
        ((other.as_str(), INCARNATION), 2),
        ((PRINCIPAL, elsewhere.as_str()), 3),
        (BINDING, 4 + SPAN_SECONDS),
    ] {
        let send = book
            .start("team_1", binding, &started("m"), now)
            .expect("started");
        assert_eq!(
            book.get("team_1", Some(&send)).expect("span").sends.len(),
            1,
            "{binding:?}"
        );
        book.start("team_1", binding, &started("m"), now)
            .expect("started");
    }
    let send = start(&book, "team_1", "m", 4 + SPAN_SECONDS);
    book.refuse("team_1", &send, "routine-x");
    let fresh = start(&book, "team_1", "m", 5 + SPAN_SECONDS);
    let found = book.get("team_1", Some(&fresh)).expect("span");
    assert_eq!((found.sends.len(), found.refused), (1, None));
}

fn spent(duration_ms: i64, models: &[(&str, i64, i64)]) -> Spent {
    let models = models
        .iter()
        .map(|(model, input, output)| ("openai".to_owned(), (*model).to_owned(), *input, *output));
    Spent {
        duration_ms,
        models: models.collect(),
    }
}

#[test]
fn a_creation_counts_each_committed_send_and_the_one_making_its_card() {
    let book = book();
    let model = json(r#"{"seq":9,"phase":"model","state":"finished","elapsed_ms":700}"#);
    let first = start(&book, "team_1", "first", 1);
    book.stepped(
        "team_1",
        &first,
        &json(r#"{"seq":1,"phase":"model","state":"started"}"#),
    );
    book.stepped("team_1", &first, &model);
    book.committed("team_1", &first, spent(1_000, &[("gpt-6.1-sol", 100, 10)]));
    // A send whose reply never committed shows neither its steps nor its usage.
    let failed = start(&book, "team_1", "failed", 2);
    book.stepped(
        "team_1",
        &failed,
        &json(r#"{"seq":9,"phase":"model","state":"finished","elapsed_ms":5}"#),
    );
    let latest = start(&book, "team_1", "latest", 3);
    let action = r#""phase":"action","state":"finished","elapsed_ms":40,"assistant_id":"shimpz-cloudflare","index":1,"action":"list-zones","total":1"#;
    book.stepped(
        "team_1",
        &latest,
        &json(&format!(r#"{{"seq":1,{action}}}"#)),
    );
    let span = book.get("team_1", Some(&latest)).expect("span");
    let kept: Vec<usize> = span
        .receipts
        .iter()
        .map(|(_, receipt)| receipt.steps.len())
        .collect();
    assert_eq!(
        kept,
        [1, 0, 1],
        "the send that never committed kept nothing once the next one opened"
    );
    let current = spent(2_500, &[("gpt-6.1-sol", 50, 5), ("gpt-6-luna", 7, 1)]);
    let (usage, steps) = creation(&span, Some(&current));
    let expected = r#"{"duration_ms":3500,"models":[{"provider":"openai","model":"gpt-6-luna","input_tokens":7,"output_tokens":1},{"provider":"openai","model":"gpt-6.1-sol","input_tokens":150,"output_tokens":15}]}"#;
    assert_eq!(usage, Some(json(expected)));
    let first_step = json(r#"{"seq":1,"phase":"model","state":"finished","elapsed_ms":700}"#);
    assert_eq!(
        steps,
        vec![first_step, json(&format!(r#"{{"seq":2,{action}}}"#))]
    );
    // A turn that reported no model usage makes a creation without its own usage.
    let alone = creation(&span, None).0.expect("usage");
    assert_eq!(alone.get("duration_ms"), Some(&json("1000")));
}

#[test]
fn a_lone_send_without_usage_or_steps_shows_no_creation() {
    let lone = book();
    let send = start(&lone, "team_1", "m", 1);
    let span = lone.get("team_1", Some(&send)).expect("span");
    assert_eq!(
        creation(
            &span,
            Some(&Spent {
                duration_ms: 10,
                models: Vec::new()
            })
        ),
        (None, Vec::new())
    );
}

#[test]
fn a_span_lists_at_most_its_creation_steps_and_drops_them_with_their_send() {
    let book = book();
    let step = json(r#"{"seq":1,"phase":"team-context","state":"finished","elapsed_ms":1}"#);
    let mut sends = Vec::new();
    for index in 0..2 {
        let send = start(&book, "team_1", &format!("send {index}"), index);
        for _ in 0..MAX_CREATION_STEPS {
            book.stepped("team_1", &send, &step);
        }
        book.committed("team_1", &send, Spent::default());
        sends.push(send);
    }
    let span = book
        .get("team_1", sends.last().map(String::as_str))
        .expect("span");
    assert_eq!(creation_steps(&span).len(), MAX_CREATION_STEPS);
    let kept: Vec<usize> = span
        .receipts
        .iter()
        .map(|(_, receipt)| receipt.steps.len())
        .collect();
    assert_eq!(kept, [MAX_CREATION_STEPS, 0]);
    // A send the span gives way to takes its receipt with it, so the newest send's steps are kept again.
    let mut latest = String::new();
    for index in 0..MAX_SENDS - 1 {
        latest = start(
            &book,
            "team_1",
            &format!("more {index}"),
            2 + i64::try_from(index).expect("index"),
        );
    }
    let span = book.get("team_1", Some(&latest)).expect("span");
    let ids: Vec<&String> = span.receipts.iter().map(|(id, _)| id).collect();
    assert_eq!(ids, span.send_ids.iter().collect::<Vec<_>>());
    assert!(!span.send_ids.contains(&sends[0]));
    book.stepped("team_1", &latest, &step);
    assert_eq!(
        creation_steps(&book.get("team_1", Some(&latest)).expect("span")).len(),
        1
    );
}

#[test]
fn abandoned_sends_never_hold_more_steps_than_the_bound_between_them() {
    let step = json(r#"{"seq":1,"phase":"team-context","state":"finished","elapsed_ms":1}"#);
    // Sends whose replies never committed never hold more steps than the bound between them.
    let idle = book();
    let mut abandoned = String::new();
    for index in 0..3 {
        abandoned = start(&idle, "team_1", &format!("abandoned {index}"), index);
        for _ in 0..MAX_CREATION_STEPS {
            idle.stepped("team_1", &abandoned, &step);
        }
    }
    let span = idle.get("team_1", Some(&abandoned)).expect("span");
    let kept: Vec<usize> = span
        .receipts
        .iter()
        .map(|(_, receipt)| receipt.steps.len())
        .collect();
    assert_eq!(kept, [0, 0, MAX_CREATION_STEPS]);
}

#[test]
fn a_span_is_live_only_until_fifteen_minutes_after_its_latest_send() {
    let clock = Arc::new(FakeClock::at(
        SystemTime::UNIX_EPOCH + Duration::from_secs(100),
    ));
    let book = book_at(&clock);
    let send = start(&book, "team_1", "m", 100);
    clock.advance(Duration::from_secs(SPAN_SECONDS.unsigned_abs()));
    assert!(book.live("team_1", Some(&send)).is_some());
    clock.advance(Duration::from_secs(1));
    assert!(book.live("team_1", Some(&send)).is_none());
    assert!(book.live("team_1", Some(&"f".repeat(32))).is_none());
    assert!(book.get("team_1", Some(&send)).is_some());
}

#[test]
fn the_oldest_sends_give_way_and_a_send_that_alone_outgrows_its_bound_refuses() {
    let book = book();
    let large = occurrence(trace::MAX_KEPT_BYTES - 64);
    let mut send = String::new();
    for index in 0..4 {
        send = start(&book, "team_1", &format!("send {index}"), index);
        book.occurred("team_1", &send, large.clone());
        book.occurred("team_1", &send, large.clone());
    }
    let found = book.get("team_1", Some(&send)).expect("span");
    let messages: Vec<&str> = found
        .sends
        .iter()
        .map(|item| item.message.as_str())
        .collect();
    assert_eq!((messages, found.refused), (vec!["send 2", "send 3"], None));
    for _ in 0..3 {
        book.occurred("team_1", &send, large.clone());
    }
    let found = book.get("team_1", Some(&send)).expect("span");
    assert_eq!(
        (found.refused.as_deref(), found.sends.len()),
        (Some("routine-recording-too-large"), 1)
    );
    book.occurred("team_1", &send, large);
    assert_eq!(
        book.get("team_1", Some(&send)).expect("span").sends,
        found.sends
    );
    for index in 0..MAX_SENDS + 2 {
        send = start(
            &book,
            "team_2",
            &format!("send {index}"),
            i64::try_from(index).expect("index"),
        );
    }
    assert_eq!(
        book.get("team_2", Some(&send)).expect("span").sends.len(),
        MAX_SENDS
    );
    let text = "x".repeat(MAX_TEXT_BYTES / 2);
    start(&book, "team_3", &text, 1);
    let send = start(&book, "team_3", &text, 2);
    assert_eq!(
        book.get("team_3", Some(&send)).expect("span").sends.len(),
        1
    );
    let alone = start(&book, "team_4", &"y".repeat(MAX_TEXT_BYTES), 1);
    let refused = book.get("team_4", Some(&alone)).expect("span").refused;
    assert_eq!(refused.as_deref(), Some("routine-recording-too-large"));
}

#[test]
fn the_brain_learns_the_one_schedule_and_output_the_person_states() {
    assert_eq!((stated_schedule(None), stated_output(None)), (None, None));
    let schedules = [
        (
            "Liste o DNS a cada 30 minutos",
            Some(json(r#"{"kind":"continuous","gap":1800}"#)),
        ),
        ("Liste o DNS", None),
        ("Liste o DNS a cada hora ou todo dia às 9h", None),
    ];
    for (message, expected) in schedules {
        let book = book();
        let send = start(&book, "team_1", message, 1);
        assert_eq!(
            stated_schedule(book.get("team_1", Some(&send)).as_ref()),
            expected,
            "{message}"
        );
    }
    let outputs = [
        ("Liste o DNS e mostre sempre".to_owned(), Some("show")),
        ("Liste o DNS".to_owned(), None),
        (
            "Liste o DNS e mostre os resultados, ou só quando mudar".to_owned(),
            None,
        ),
        (
            compose_clarified("Liste o DNS", "E o resultado?", "Não mostrar", "pt")
                .expect("composed"),
            Some("none"),
        ),
    ];
    for (message, expected) in outputs {
        let book = book();
        let send = start(&book, "team_1", &message, 1);
        let found = stated_output(book.get("team_1", Some(&send)).as_ref());
        assert_eq!(found.as_deref(), expected, "{message}");
    }
}

#[test]
fn a_question_is_kept_and_counts_sends_as_old_ones_go() {
    let book = book();
    start(&book, "team_1", "m", 1);
    let first = start(&book, "team_1", "m", 2);
    let mut question = Question::new("routine-work-rerun", Vec::new());
    question.manifest = Some(Manifest::default());
    book.asked("team_1", &first, &question, intent(None));
    let found = book.get("team_1", Some(&first)).expect("span");
    let asked = found.asked.expect("asked");
    assert_eq!(
        (asked.after, asked.wire.is_some(), found.intent),
        (2, true, Some(intent(None)))
    );
    let mut send = String::new();
    for index in 0..MAX_SENDS {
        send = start(
            &book,
            "team_1",
            "m",
            3 + i64::try_from(index).expect("index"),
        );
    }
    assert_eq!(
        book.get("team_1", Some(&send))
            .expect("span")
            .asked
            .expect("asked")
            .after,
        0
    );
}

#[test]
fn a_record_intent_keeps_the_work_it_selected_until_a_send_runs_actions_again() {
    let book = book();
    let worked = start(&book, "team_1", "m", 1);
    book.occurred("team_1", &worked, occurrence(0));
    let idle = start(&book, "team_1", "m", 2);
    let span = book.get("team_1", Some(&idle)).expect("span");
    let other_routine = "a".repeat(32);
    // A first record call that ran nothing selects the latest work; a replacement then keeps its own steps.
    assert_eq!(continued(&span, None), None);
    assert_eq!(selected_work(&span, None), Some(worked.clone()));
    assert_eq!(selected_work(&span, Some(&other_routine)), None);
    let kept = intent(Some(&worked));
    book.asked(
        "team_1",
        &idle,
        &Question::new("routine-output-unstated", Vec::new()),
        kept.clone(),
    );
    // An unrelated send that ran Actions never replaces the intent's work for an answer that runs nothing.
    let other = start(&book, "team_1", "m", 3);
    book.occurred("team_1", &other, occurrence(0));
    let answered = start(&book, "team_1", "m", 4);
    let span = book.get("team_1", Some(&answered)).expect("span");
    assert_eq!(continued(&span, None), Some(kept));
    assert_eq!(continued(&span, Some(&other_routine)), None);
    // A send that ran Actions starts a new intent, which selects that send.
    book.occurred("team_1", &answered, occurrence(0));
    let ran = book.get("team_1", Some(&answered)).expect("span");
    assert_eq!(
        (continued(&ran, None), selected_work(&ran, None)),
        (None, Some(answered.clone()))
    );
    let nothing = start(&book, "team_2", "m", 5);
    assert_eq!(
        selected_work(&book.get("team_2", Some(&nothing)).expect("span"), None),
        None
    );
}

fn entry(role: &str, text: &str, truncated: bool) -> ConversationEntry {
    ConversationEntry {
        role: role.to_owned(),
        text: text.to_owned(),
        truncated,
    }
}

#[test]
fn only_the_persons_own_untruncated_earlier_lines_join_the_send() {
    let composed = "A cada hora\n\nPergunta: Qual?\nResposta: shimpz.com";
    let conversation = vec![
        entry("user", "DNS de shimpz.com", false),
        entry("assistant", "zona other.org", false),
        entry("user", "zona blog.dev cortada", true),
        entry("user", composed, false),
    ];
    assert_eq!(
        earlier_sends(&conversation),
        ["DNS de shimpz.com", composed]
    );
    let book = book();
    let request = Started {
        message: "Faça isso".to_owned(),
        conversation,
        timezone: Some("Europe/Lisbon".to_owned()),
        locale: None,
    };
    let send = book.start("team_1", BINDING, &request, 1).expect("started");
    let found = book
        .get("team_1", Some(&send))
        .expect("span")
        .sends
        .remove(0);
    assert_eq!(found.person, ["Faça isso"]);
    assert_eq!(
        found.window,
        ["DNS de shimpz.com", "A cada hora", "shimpz.com"]
    );
    assert_eq!(found.timezone.as_deref(), Some("Europe/Lisbon"));
}

fn answered_by(code: &str, message: &str, kept: Option<Intent>) -> Option<Intent> {
    let pending = Pending {
        action: ("dns".to_owned(), "records".to_owned()),
        member: "zone_id".to_owned(),
        targets: vec![
            Target::new(json(r#""a-zone""#), None),
            Target::new(json("123"), None),
        ],
        chosen: None,
    };
    let book = book();
    let send = start(&book, "team_1", "Liste", 1);
    let mut question = Question::new(code, Vec::new());
    question.pending = Some(pending);
    let absent = kept.is_none();
    book.asked(
        "team_1",
        &send,
        &question,
        kept.unwrap_or_else(|| intent(None)),
    );
    if absent {
        book.change("team_1", &send, |mut found| {
            found.intent = None;
            found
        });
    }
    let send = start(&book, "team_1", message, 2);
    answered(book.get("team_1", Some(&send)).as_ref())
}

#[test]
fn only_a_composed_answer_that_binds_the_pending_question_is_recorded_without_the_brain() {
    let bound = Some(intent(None));
    let cases = [
        ("routine-schedule-unstated", "a cada 30 segundos", None),
        (
            "routine-output-unstated",
            "Mostrar somente quando mudar",
            None,
        ),
        ("routine-interval-over-budget", "a cada 31 segundos", None),
        ("routine-binding-ambiguous", r#""a-zone""#, bound.clone()),
        ("routine-binding-ambiguous", "123", bound.clone()),
        ("routine-binding-ambiguous", "a-zone", None),
        ("routine-binding-ambiguous", r#""123""#, None),
        ("routine-work-rerun", "a cada hora", None),
        // A target answer that also states a schedule is the Brain's to read.
        (
            "routine-binding-ambiguous",
            r#""a-zone". A cada 60 segundos."#,
            None,
        ),
    ];
    for (code, text, expected) in cases {
        assert_eq!(
            answered_by(code, &answer("Liste", text), bound.clone()),
            expected,
            "{code} {text}"
        );
    }
    // Only Admin's complete composition, ending in its answer, is an answer Team records itself.
    for message in [
        r#""a-zone""#,
        "Resposta: \"a-zone\"",
        "Liste\nPergunta: Qual?\nResposta: \"a-zone\"",
        "Liste\n\nPergunta: Qual?\nResposta: \"a-zone\"\nAgora liste as zonas",
    ] {
        assert_eq!(
            answered_by("routine-binding-ambiguous", message, bound.clone()),
            None,
            "{message}"
        );
    }
    assert_eq!(
        answered_by(
            "routine-binding-ambiguous",
            &answer("Liste", r#""a-zone""#),
            None
        ),
        None
    );
    assert_eq!(answered(None), None);
}

fn settings_by(code: &str, text: &str, value: Option<i64>, composed: bool) -> bool {
    let book = book();
    let send = start(&book, "team_1", "Liste", 1);
    let mut question = Question::new(code, Vec::new());
    question.value = value;
    book.asked("team_1", &send, &question, intent(None));
    let message = if composed {
        answer("Liste", text)
    } else {
        text.to_owned()
    };
    let send = start(&book, "team_1", &message, 2);
    settings_answer(book.get("team_1", Some(&send)).as_ref())
}

#[test]
fn only_a_fixed_answer_to_a_settings_question_records_alone() {
    let over = "routine-interval-over-budget";
    for (code, text, value) in [
        (
            "routine-output-unstated",
            "Mostrar somente quando mudar",
            None,
        ),
        ("routine-output-unstated", "Show only when it changes", None),
        ("routine-schedule-unstated", "A cada hora", None),
        ("routine-schedule-unstated", "毎日 09:00", None),
        (over, "A cada 120 segundos", Some(120)),
        (over, "Every 120 seconds", Some(120)),
    ] {
        assert!(settings_by(code, text, value, true), "{code} {text}");
    }
    for (code, text, value, composed) in [
        (
            "routine-output-unstated",
            "Mostrar somente quando mudar e liste example.com",
            None,
            true,
        ),
        ("routine-output-unstated", "A cada hora", None, true),
        (
            "routine-schedule-unstated",
            "A cada hora; e apague o registro www",
            None,
            true,
        ),
        ("routine-schedule-unstated", "A cada 2 horas", None, true),
        ("routine-schedule-unstated", "A cada hora", None, false),
        (over, "A cada 60 segundos", Some(120), true),
        ("routine-binding-ambiguous", r#""shimpz.com""#, None, true),
        ("routine-work-rerun", "Executar de novo", None, true),
    ] {
        assert!(!settings_by(code, text, value, composed), "{code} {text}");
    }
    assert!(!settings_answer(None));
}

#[test]
fn routine_mode_follows_schedule_words_an_intent_a_question_or_a_selection() {
    let asked = answer(
        &answer("Cria uma rotina pra mim", "Listar registros DNS"),
        "A cada 30 segundos",
    );
    let cases = [
        (asked.as_str(), true),
        ("Explique o job que roda a cada hora", true),
        ("Liste os registros DNS de shimpz.com", false),
        (
            "cria uma rotina pra mim, que lista os registros dns do dominio shimpz.com",
            true,
        ),
        ("Não quero uma rotina, só liste os registros", false),
    ];
    for (message, expected) in cases {
        let book = book();
        let send = start(&book, "team_1", message, 1);
        assert_eq!(
            routine_mode(book.get("team_1", Some(&send)).as_ref()),
            expected,
            "{message}"
        );
    }
    let book = book();
    let send = start(&book, "team_1", "Liste", 1);
    book.asked(
        "team_1",
        &send,
        &Question::new("routine-work-rerun", Vec::new()),
        intent(None),
    );
    assert!(routine_mode(book.get("team_1", Some(&send)).as_ref()));
    let selected = start(&book, "team_2", "Liste", 1);
    let routine = "a".repeat(32);
    book.listed(
        "team_2",
        &selected,
        vec![(routine.clone(), 1)],
        &Referred::Selected(routine),
    );
    assert!(routine_mode(book.get("team_2", Some(&selected)).as_ref()));
    assert!(!routine_mode(None));
}

/// A book whose one span listed two Routines, with what its send says about them (`test_routine_reference.py`).
fn span_book(reference: &Referred) -> (RecordingBook, String) {
    let book = book();
    let send = start(&book, "team_1", "m", 1);
    let listed = vec![("a".repeat(32), 2), ("b".repeat(32), 1)];
    book.listed("team_1", &send, listed, reference);
    (book, send)
}

#[test]
fn a_send_that_names_nothing_keeps_the_spans_selection_while_it_is_listed() {
    let (first_routine, other) = ("a".repeat(32), "b".repeat(32));
    let (book, first) = span_book(&Referred::Selected(first_routine.clone()));
    let span = book.get("team_1", Some(&first));
    assert_eq!(
        span.as_ref().and_then(|span| span.selected.clone()),
        Some(first_routine.clone())
    );
    assert!(routine_mode(span.as_ref()));
    let second = start(&book, "team_1", "m", 2);
    book.listed(
        "team_1",
        &second,
        vec![(first_routine.clone(), 3)],
        &Referred::Nothing,
    );
    assert_eq!(
        book.get("team_1", Some(&second)).expect("span").selected,
        Some(first_routine)
    );
    let third = start(&book, "team_1", "m", 3);
    book.listed("team_1", &third, vec![(other, 1)], &Referred::Nothing);
    assert_eq!(
        book.get("team_1", Some(&third)).expect("span").selected,
        None
    );
}

#[test]
fn another_routine_or_an_unresolved_reference_replaces_the_selection() {
    let (first_routine, other) = ("a".repeat(32), "b".repeat(32));
    let listed = vec![(first_routine.clone(), 2), (other.clone(), 1)];
    let (book, first) = span_book(&Referred::Selected(first_routine));
    let second = start(&book, "team_1", "m", 2);
    book.listed(
        "team_1",
        &second,
        listed.clone(),
        &Referred::Selected(other.clone()),
    );
    assert_eq!(
        book.get("team_1", Some(&second)).expect("span").selected,
        Some(other)
    );
    let third = start(&book, "team_1", "m", 3);
    book.listed("team_1", &third, listed, &Referred::Unresolved);
    let span = book.get("team_1", Some(&third));
    assert_eq!(span.as_ref().and_then(|span| span.selected.clone()), None);
    assert!(!routine_mode(span.as_ref()));
    assert!(book.get("team_1", Some(&first)).is_none());
}

fn traced(interrupt: &str) -> Occurrence {
    let mut item = occurrence(0);
    interrupt.clone_into(&mut item.interrupt);
    item
}

#[test]
fn results_name_the_traced_calls_only_while_a_routine_is_selected() {
    let (book, send) = span_book(&Referred::Nothing);
    book.occurred("team_1", &send, traced("i-1"));
    book.occurred("team_1", &send, traced("i-3"));
    let results = json(r#"{"i-1":{"zones":[]},"i-2":{"error":"failed"},"i-3":[1]}"#);
    assert_eq!(
        labeled(book.get("team_1", Some(&send)).as_ref(), results.clone()),
        results
    );
    assert_eq!(labeled(None, results.clone()), results);
    book.listed(
        "team_1",
        &send,
        vec![("a".repeat(32), 2)],
        &Referred::Selected("a".repeat(32)),
    );
    let expected = r#"{"i-1":{"call":"c1","result":{"zones":[]}},"i-2":{"error":"failed"},"i-3":{"call":"c2","result":[1]}}"#;
    assert_eq!(
        labeled(book.get("team_1", Some(&send)).as_ref(), results),
        json(expected)
    );
}

fn rerun_span(slots: Vec<Slot>, protected: &[&str]) -> Span {
    let mut question = Question::new(RERUN, Vec::new());
    question.manifest = Some(Manifest { slots });
    let book = book();
    let send = start(&book, "team_1", "m", 1);
    book.asked("team_1", &send, &question, intent(None));
    let mut span = book.get("team_1", Some(&send)).expect("span");
    span.protection = Protection::default().grow(protected.iter().copied());
    span
}

fn slot(action: (&str, &str), inputs: Vec<(&str, &str, Value)>) -> Slot {
    Slot {
        action: (action.0.to_owned(), action.1.to_owned()),
        inputs: inputs
            .into_iter()
            .map(|(member, kind, value)| (member.to_owned(), kind.to_owned(), value))
            .collect(),
        sources: Vec::new(),
    }
}

#[test]
fn a_protected_value_is_withheld_even_where_json_escaping_would_hide_it() {
    let secret = "tok\"en\\1";
    let note = object_of(vec![("text", Value::String(format!("use {secret} now")))]);
    let inputs = vec![("note", "value", note), ("t", "value", json(r#""x""#))];
    let span = rerun_span(vec![slot(("reports", "post"), inputs)], &[secret]);
    let shown = rerun_work(Some(&span)).expect("shown");
    let values: Vec<Option<&Value>> =
        shown[0]
            .get("inputs")
            .map_or(Vec::new(), |inputs| match inputs {
                Value::Array(items) => items.iter().map(|item| item.get("value")).collect(),
                _ => Vec::new(),
            });
    assert_eq!(values, [Some(&Value::Null), Some(&json(r#""\"x\"""#))]);
    let mut lost = span;
    lost.protection = Protection {
        lost: true,
        ..Protection::default()
    };
    assert_eq!(rerun_work(Some(&lost)), None);
    assert_eq!(rerun_work(None), None);
}

#[test]
fn consecutive_identical_calls_count_once_and_work_past_the_wire_bounds_is_never_shown() {
    let many = slot(("reports", "post"), vec![("t", "value", json(r#""x""#))]);
    let mut fresh = slot(
        ("cloudflare", "list-dns-records"),
        vec![("zone_id", "fresh", Value::Null)],
    );
    fresh.sources = vec![(
        "zone_id".to_owned(),
        ("cloudflare".to_owned(), "list-zones".to_owned()),
    )];
    let shown = rerun_work(Some(&rerun_span(
        vec![many.clone(), many.clone(), fresh],
        &[],
    )))
    .expect("shown");
    assert_eq!(shown.len(), 2);
    assert_eq!(shown[0].get("count"), Some(&json("2")));
    let source = shown[1].get("inputs").and_then(|inputs| match inputs {
        Value::Array(items) => items.first().and_then(|item| item.get("source")).cloned(),
        _ => None,
    });
    assert_eq!(
        source,
        Some(json(r#"{"assistant":"cloudflare","action":"list-zones"}"#))
    );
    let mut wide = slot(("reports", "fetch"), Vec::new());
    wide.inputs = (0..65)
        .map(|index| (format!("m{index:03}"), "clock".to_owned(), Value::Null))
        .collect();
    assert_eq!(rerun_work(Some(&rerun_span(vec![wide], &[]))), None);
    assert_eq!(
        rerun_work(Some(&rerun_span(vec![many.clone(); 257], &[]))),
        None
    );
    let ordinary = rerun_work(Some(&rerun_span(vec![many; 256], &[]))).expect("shown");
    assert_eq!(
        canonical_rerun(&Value::Array(ordinary.clone())),
        Some(&Value::Array(ordinary))
    );
}

#[test]
fn dropping_a_team_or_clearing_forgets_every_span() {
    let book = book();
    let first = start(&book, "team_1", "m", 1);
    let second = start(&book, "team_2", "m", 1);
    book.drop_team("team_1");
    assert!(book.get("team_1", Some(&first)).is_none());
    book.clear();
    assert!(book.get("team_2", Some(&second)).is_none());
}
