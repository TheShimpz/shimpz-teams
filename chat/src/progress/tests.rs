use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use strict_json::Value;
use team_protocol::progress::{MAX_EVENTS, canonical_event};

use super::{MonotonicNs, Phase, Position, Reporter, Sink};

fn collected() -> (Sink, Arc<Mutex<Vec<Value>>>) {
    let events = Arc::new(Mutex::new(Vec::new()));
    let kept = Arc::clone(&events);
    let sink: Sink = Arc::new(move |event| kept.lock().expect("events").push(event));
    (sink, events)
}

fn ticks(values: &[i64]) -> MonotonicNs {
    let values = Mutex::new(VecDeque::from(values.to_owned()));
    Arc::new(move || values.lock().expect("ticks").pop_front().unwrap_or(0))
}

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

#[test]
fn the_reporter_emits_only_closed_measured_operation_pairs() {
    let (sink, events) = collected();
    let reporter = Reporter::with_clock(sink, ticks(&[1_000_000_000, 1_012_000_000]), 0);
    drop(reporter.span(
        Phase::Action,
        Some(Position {
            index: 1,
            total: 2,
            assistant_id: "cloudflare-helper".to_owned(),
            action: "list-zones".to_owned(),
        }),
    ));
    let events = events.lock().expect("events").clone();
    assert_eq!(
        events,
        vec![
            json(
                r#"{"seq":1,"phase":"action","state":"started","index":1,"total":2,
                "assistant_id":"cloudflare-helper","action":"list-zones"}"#
            ),
            json(
                r#"{"seq":2,"phase":"action","state":"finished","elapsed_ms":12,"index":1,"total":2,
                "assistant_id":"cloudflare-helper","action":"list-zones"}"#
            ),
        ]
    );
    for event in &events {
        assert!(canonical_event(event).is_ok());
    }
}

#[test]
fn a_silent_reporter_counts_nothing_and_a_span_still_ends() {
    let reporter = Reporter::silent();
    drop(reporter.span(Phase::Model, None));
    assert_eq!(reporter.sequence(), 0);
}

#[test]
fn the_reporter_never_emits_an_unpaired_span_at_its_sequence_cap() {
    let (sink, events) = collected();
    let reporter = Reporter::with_clock(sink, ticks(&[]), MAX_EVENTS - 1);
    drop(reporter.span(Phase::Model, None));
    assert_eq!(events.lock().expect("events").len(), 0);
    assert_eq!(reporter.sequence(), MAX_EVENTS - 1);
}

#[test]
fn an_invalid_position_emits_nothing() {
    let (sink, events) = collected();
    let reporter = Reporter::with_clock(sink, ticks(&[]), 0);
    let position = Position {
        index: 3,
        total: 2,
        assistant_id: "cloudflare-helper".to_owned(),
        action: "list-zones".to_owned(),
    };
    drop(reporter.span(Phase::Action, Some(position.clone())));
    drop(reporter.span(
        Phase::Model,
        Some(Position {
            index: 1,
            ..position
        }),
    ));
    drop(reporter.span(Phase::Action, None));
    assert_eq!(events.lock().expect("events").len(), 0);
}

#[test]
fn a_teed_reporter_continues_the_sequence_into_both_sinks() {
    let (first, first_events) = collected();
    let (second, second_events) = collected();
    let reporter = Reporter::with_clock(first, ticks(&[0, 0, 0, 0]), 0);
    drop(reporter.span(Phase::Model, None));
    let teed = reporter.teed(second);
    drop(teed.span(Phase::TeamContext, None));
    assert_eq!(first_events.lock().expect("events").len(), 4);
    let second_events = second_events.lock().expect("events").clone();
    assert_eq!(second_events.len(), 2);
    assert_eq!(second_events[0].get("seq"), Some(&json("3")));
    assert_eq!(reporter.sequence(), 4);
}
