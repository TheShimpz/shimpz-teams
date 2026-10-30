use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_protocol::payload::{apply_memory_changes, canonical_memory_changes};

use super::{
    Change, EVICTED, Entry, FORGET, Plan, REMEMBER, Source, canonical_entries, canonical_source,
    plan, projection,
};

const VECTORS: &str = include_str!("../../../protocol/http/v1/vectors.json");

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn source() -> Source {
    Source {
        nonce: "b".repeat(32),
        actor_id: "a".repeat(32),
        incarnation: "c".repeat(64),
    }
}

fn changes(text: &str) -> Vec<Value> {
    match canonical_memory_changes(&json(text)).expect("canonical changes") {
        Value::Array(items) => items,
        _ => unreachable!(),
    }
}

fn entry(index: usize, topic: &str, preference: &str) -> Entry {
    Entry {
        memory_id: format!("{index:032x}"),
        topic: topic.to_owned(),
        preference: preference.to_owned(),
        since: "2026-10-21T00:00:00Z".to_owned(),
        source: source(),
    }
}

#[test]
fn the_plan_applies_changes_exactly_as_the_protocol_vectors() {
    let vectors = json(VECTORS);
    let Some(Value::Array(cases)) = vectors.get("memory_apply") else {
        panic!("memory_apply vectors");
    };
    assert_ne!(cases.len(), 0);
    for case in cases {
        let Some(Value::Array(memory)) = case.get("memory") else {
            panic!("memory");
        };
        let entries: Vec<Entry> = memory
            .iter()
            .enumerate()
            .map(|(index, item)| {
                let text = |name: &str| match item.get(name) {
                    Some(Value::String(text)) => text.clone(),
                    _ => panic!("{name}"),
                };
                entry(index, &text("topic"), &text("preference"))
            })
            .collect();
        let Some(Value::Array(changed)) = case.get("changes") else {
            panic!("changes");
        };
        let planned = plan(
            &entries,
            changed,
            &source(),
            "2026-10-21T00:00:01Z",
            &OsEntropy,
        )
        .expect("plan");
        let result = case.get("result").expect("result");
        assert_eq!(&Value::Array(projection(&planned.memory)), result);
        let raw_changes = case.get("changes").expect("changes");
        assert_eq!(
            &apply_memory_changes(case.get("memory").expect("memory"), raw_changes),
            result
        );
    }
}

fn remembered() -> (Entry, Entry) {
    let first = plan(
        &[],
        &changes(
            r#"[{"op":"remember","topic":"language","preference":"Answer in Portuguese."},
            {"op":"remember","topic":"format","preference":"Use short lists."}]"#,
        ),
        &source(),
        "2026-10-21T00:00:00Z",
        &OsEntropy,
    )
    .expect("plan");
    let [language, format] = first.memory.as_slice() else {
        panic!("two entries");
    };
    assert_ne!(language.memory_id, format.memory_id);
    assert_eq!(
        first.changes,
        vec![
            Change {
                operation: REMEMBER,
                memory_id: language.memory_id.clone()
            },
            Change {
                operation: REMEMBER,
                memory_id: format.memory_id.clone()
            },
        ]
    );
    (language.clone(), format.clone())
}

#[test]
fn a_remembered_entry_is_stamped() {
    let (language, format) = remembered();
    assert_eq!(language.source, source());
    assert_eq!(format.since, "2026-10-21T00:00:00Z");
}

#[test]
fn a_reused_topic_gets_a_new_identity_and_forget_removes_it_whole() {
    let (language, format) = remembered();
    let first = Plan {
        memory: vec![language.clone(), format.clone()],
        changes: Vec::new(),
    };
    let format = &format;
    let other = Source {
        actor_id: "d".repeat(32),
        ..source()
    };
    let second = plan(
        &first.memory,
        &changes(r#"[{"op":"remember","topic":"language","preference":"Answer in English."}]"#),
        &other,
        "2100-01-01T00:00:00Z",
        &OsEntropy,
    )
    .expect("plan");
    let [kept, replaced] = second.memory.as_slice() else {
        panic!("two entries");
    };
    assert_eq!(kept, format);
    assert_ne!(replaced.memory_id, language.memory_id);
    assert_eq!(
        (replaced.since.as_str(), &replaced.source),
        ("2100-01-01T00:00:00Z", &other)
    );
    let forgotten = plan(
        &second.memory,
        &changes(
            r#"[{"op":"forget","topic":"format","preference":""},{"op":"forget","topic":"absent","preference":""}]"#,
        ),
        &source(),
        "2100-01-01T00:00:00Z",
        &OsEntropy,
    )
    .expect("plan");
    assert_eq!(
        forgotten.changes,
        vec![Change {
            operation: FORGET,
            memory_id: format.memory_id.clone()
        }]
    );
    assert_eq!(forgotten.memory, vec![replaced.clone()]);
}

#[test]
fn the_bound_evicts_the_oldest_entry_and_names_it() {
    let entries: Vec<Entry> = (0..32)
        .map(|index| entry(index, &format!("t{index}"), "x"))
        .collect();
    let planned = plan(
        &entries,
        &changes(r#"[{"op":"remember","topic":"new","preference":"y"}]"#),
        &source(),
        "2026-10-21T00:00:00Z",
        &OsEntropy,
    )
    .expect("plan");
    let newest = planned.memory.last().expect("newest");
    assert_eq!(
        planned.changes,
        vec![
            Change {
                operation: REMEMBER,
                memory_id: newest.memory_id.clone()
            },
            Change {
                operation: EVICTED,
                memory_id: entries[0].memory_id.clone()
            },
        ]
    );
    assert_eq!(planned.memory.len(), 32);
}

#[test]
fn only_exact_sources_are_admitted() {
    assert_eq!(canonical_source(&source().record()), Some(source()));
    for refused in [
        "null",
        r#"{"nonce":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","actor_id":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","incarnation":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}"#,
        r#"{"nonce":"x","actor_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","incarnation":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}"#,
        r#"{"nonce":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","actor_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","incarnation":7}"#,
        r#"{"nonce":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","actor_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","incarnation":"network"}"#,
    ] {
        assert_eq!(canonical_source(&json(refused)), None, "{refused}");
    }
}

#[test]
fn only_exact_entries_are_admitted() {
    let good = entry(1, "language", "Answer in Portuguese.");
    assert_eq!(
        canonical_entries(&Value::Array(vec![good.record()])),
        Some(vec![good.clone()])
    );
    let with = |edit: &dyn Fn(&mut Entry)| {
        let mut changed = good.clone();
        edit(&mut changed);
        changed.record()
    };
    let mut extra = good.record();
    if let Value::Object(members) = &mut extra {
        members.push(("extra".to_owned(), json("1")));
    }
    let refused = [
        Value::Array(vec![with(&|entry| entry.topic = "Language".to_owned())]),
        Value::Array(vec![with(&|entry| entry.memory_id = "A".repeat(32))]),
        Value::Array(vec![with(&|entry| {
            entry.since = "2026-10-21 00:00:00".to_owned();
        })]),
        Value::Array(vec![with(&|entry| entry.source.nonce = "x".to_owned())]),
        Value::Array(vec![
            good.record(),
            with(&|entry| entry.topic = "format".to_owned()),
        ]),
        Value::Array(vec![
            good.record(),
            with(&|entry| entry.memory_id = "f".repeat(32)),
        ]),
        Value::Array(vec![extra]),
        Value::Array(vec![json(r#"{"topic":"language","preference":"x"}"#)]),
        Value::Array(vec![json(r#""not an entry""#)]),
        json(r#""not a list""#),
    ];
    for value in refused {
        assert_eq!(canonical_entries(&value), None, "{value:?}");
    }
    let other = with(&|entry| {
        entry.topic = "format".to_owned();
        entry.memory_id = "f".repeat(32);
    });
    assert!(canonical_entries(&Value::Array(vec![good.record(), other])).is_some());
}
