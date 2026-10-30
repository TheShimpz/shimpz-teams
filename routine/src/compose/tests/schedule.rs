//! When a recorded Routine runs, what it shows, and in which zone: the person's own words, read by the phrase tables.

use strict_json::Value;
use team_protocol::payload::compose_clarified;

use super::{Span, send, zones_call};
use crate::json::{at, text};
use crate::recording::{Existing, Question, RERUN, Recording};
use crate::testing::json;

fn schedule_of(messages: &[&str]) -> Value {
    let mut sends = vec![send(vec![zones_call()], messages[0])];
    sends.extend(
        messages[1..]
            .iter()
            .map(|message| send(Vec::new(), message)),
    );
    Span::of(sends).recorded().schedule
}

fn asked_schedule(message: &str) -> Question {
    Span::of(vec![send(vec![zones_call()], message)]).question()
}

#[test]
fn the_latest_send_stating_a_schedule_wins_and_an_answer_wins_over_the_request_it_repeats() {
    assert_eq!(
        schedule_of(&["todo dia às 9h", "melhor a cada 30 segundos"]),
        json(r#"{"kind": "continuous", "gap": 30}"#)
    );
    let composed = compose_clarified(
        "DNS a cada 5 segundos",
        "Tem certeza?",
        "a cada 30 segundos",
        "pt",
    )
    .expect("composed");
    assert_eq!(
        schedule_of(&[&composed]),
        json(r#"{"kind": "continuous", "gap": 30}"#)
    );
}

#[test]
fn a_schedule_unstated_ambiguous_or_only_asked_is_asked_again() {
    let unstated = Question::new("routine-schedule-unstated", Vec::new());
    // Different schedules in one authored text are asked about, even across lines; so is an unreadable count.
    let unreadable = format!("every {} seconds", "9".repeat(5000));
    for message in [
        "Run every 5 seconds.\nRun every 30 seconds.",
        unreadable.as_str(),
        "Cria uma rotina",
        "a cada hora e todo dia às 9h",
        "Todo dia às 9h?",
    ] {
        assert_eq!(asked_schedule(message), unstated, "{message}");
    }
}

#[test]
fn a_replacement_keeps_its_schedule_unless_one_is_stated() {
    let existing = Existing {
        plan: json(r#"{"steps": []}"#),
        schedule: json(r#"{"kind": "daily", "time": "08:00"}"#),
    };
    let replaced = |message: &str| {
        Span {
            existing: Some(existing.clone()),
            ..Span::of(vec![send(vec![zones_call()], message)])
        }
        .recorded()
    };
    assert_eq!(replaced("sem mudar o horário").schedule, existing.schedule);
    assert_eq!(
        replaced("a cada hora").schedule,
        json(r#"{"kind": "continuous", "gap": 3600}"#)
    );
}

/// The output a span records, or the question it asks, reading the person's own words.
fn output(span: Span) -> Result<Value, Box<Question>> {
    match (Span { mode: None, ..span }).record() {
        Ok(Recording::Recorded(recorded)) => Ok(at(&recorded.document, "output").clone()),
        Ok(Recording::Asked(question)) => Err(question),
        Err(error) => panic!("refused: {error}"),
    }
}

#[test]
fn the_latest_segment_stating_an_output_wins_and_a_chain_shows() {
    for (message, expected) in [
        (
            "a cada hora, mostrar sempre",
            r#"{"mode": "show", "step": "s1"}"#,
        ),
        (
            "a cada hora, só quando mudar",
            r#"{"mode": "changes", "step": "s1"}"#,
        ),
        (
            "a cada hora, não precisa mostrar",
            r#"{"mode": "none", "step": null}"#,
        ),
    ] {
        assert_eq!(
            output(Span::of(vec![send(vec![zones_call()], message)])),
            Ok(json(expected)),
            "{message}"
        );
    }
    let later = output(Span::of(vec![
        send(vec![zones_call()], "a cada hora, mostrar sempre"),
        send(Vec::new(), "só quando mudar"),
    ]));
    assert_eq!(
        later.map(|found| at(&found, "mode").clone()),
        Ok(text("changes"))
    );
}

#[test]
fn no_output_or_two_in_one_segment_are_asked_never_guessed() {
    let unstated = Question::new("routine-output-unstated", Vec::new());
    for message in [
        "a cada hora",
        "a cada hora. Mostrar sempre. Ou só quando mudar.",
    ] {
        assert_eq!(
            output(Span::of(vec![send(vec![zones_call()], message)])),
            Err(Box::new(unstated.clone())),
            "{message}"
        );
    }
    // The work's own questions come first.
    let remembered = output(Span::of(vec![send(
        vec![super::records()],
        "DNS de shimpz.com a cada hora",
    )]));
    assert_eq!(
        remembered.map_err(|question| question.code),
        Err(RERUN.to_owned())
    );
}

#[test]
fn a_replacement_keeps_its_output_unless_the_person_states_another() {
    let existing = Existing {
        plan: super::boundary::kept_plan(),
        schedule: json(r#"{"kind": "daily", "time": "08:00"}"#),
    };
    let replaced = |message: &str| {
        output(Span {
            existing: Some(existing.clone()),
            ..Span::of(vec![send(Vec::new(), message)])
        })
    };
    assert_eq!(
        replaced("ok").map(|found| at(&found, "mode").clone()),
        Ok(text("show"))
    );
    assert_eq!(
        replaced("não precisa mostrar").map(|found| at(&found, "mode").clone()),
        Ok(text("none"))
    );
    assert_eq!(
        replaced("Mostrar sempre. Ou só quando mudar."),
        Err(Box::new(Question::new(
            "routine-output-unstated",
            Vec::new()
        )))
    );
}

fn zone_of(sends: Vec<crate::recording::Send>) -> (Value, String) {
    let recorded = Span::of(sends).recorded();
    (
        at(&recorded.document, "timezone").clone(),
        recorded.timezone_source,
    )
}

fn zoned(zone: &str, source: &str) -> (Value, String) {
    (text(zone), source.to_owned())
}

#[test]
fn a_written_zone_wins_over_the_browser_and_the_latest_one_counts() {
    let latest = zone_of(vec![
        send(vec![zones_call()], "todo dia às 9h, Europe/Paris"),
        send(Vec::new(), "Europe/Lisbon"),
    ]);
    assert_eq!(latest, zoned("Europe/Lisbon", "person"));
    // A zone ending a sentence is the person's, and a partial offset is no zone.
    assert_eq!(
        zone_of(vec![send(
            vec![zones_call()],
            "Run daily at 09:00 in Europe/London."
        )]),
        zoned("Europe/London", "person")
    );
    assert_eq!(
        zone_of(vec![send(vec![zones_call()], "Run daily at 09:00 UTC+3")]),
        zoned("America/Sao_Paulo", "browser")
    );
}

#[test]
fn two_zones_in_the_latest_naming_segment_name_none_and_a_later_answer_settles_them() {
    let two = send(
        vec![zones_call()],
        "todo dia às 9h, Europe/Paris ou Europe/London",
    );
    assert_eq!(
        zone_of(vec![two.clone()]),
        zoned("America/Sao_Paulo", "browser")
    );
    assert_eq!(
        zone_of(vec![two, send(Vec::new(), "Europe/London")]),
        zoned("Europe/London", "person")
    );
    let composed = compose_clarified(
        "DNS todo dia às 9h, Europe/Paris or Europe/London",
        "Qual fuso?",
        "Europe/London",
        "en",
    )
    .expect("composed");
    assert_eq!(
        zone_of(vec![send(vec![zones_call()], &composed)]),
        zoned("Europe/London", "person")
    );
}

#[test]
fn the_latest_browser_zone_counts_and_with_none_known_it_is_utc_by_convention() {
    let mut tokyo = send(vec![zones_call()], "todo dia às 9h");
    tokyo.timezone = Some("Asia/Tokyo".to_owned());
    assert_eq!(
        zone_of(vec![tokyo, send(Vec::new(), "ok")]),
        zoned("America/Sao_Paulo", "browser")
    );
    for message in ["a cada hora", "todo dia às 9h"] {
        let mut unzoned = send(vec![zones_call()], message);
        unzoned.timezone = None;
        assert_eq!(zone_of(vec![unzoned]), zoned("UTC", "none"));
    }
}

#[test]
fn a_replacement_takes_the_requests_zone_never_the_replaced_routines() {
    let london = Existing {
        plan: json(r#"{"steps": []}"#),
        schedule: json(r#"{"kind": "daily", "time": "08:00"}"#),
    };
    let replaced = |timezone: Option<&str>| {
        let mut request = send(vec![zones_call()], "sem mudar o horário");
        request.timezone = timezone.map(str::to_owned);
        let recorded = Span {
            existing: Some(london.clone()),
            ..Span::of(vec![request])
        }
        .recorded();
        (
            at(&recorded.document, "timezone").clone(),
            recorded.timezone_source,
        )
    };
    assert_eq!(replaced(Some("Asia/Tokyo")), zoned("Asia/Tokyo", "browser"));
    assert_eq!(replaced(None), zoned("UTC", "none"));
    let hourly = Existing {
        plan: json(r#"{"steps": []}"#),
        schedule: json(r#"{"kind": "continuous", "gap": 3600}"#),
    };
    let mut request = super::hourly(vec![zones_call()]);
    request.timezone = Some("Asia/Tokyo".to_owned());
    let recorded = Span {
        existing: Some(hourly),
        ..Span::of(vec![request])
    }
    .recorded();
    assert_eq!(at(&recorded.document, "timezone"), &text("Asia/Tokyo"));
}
