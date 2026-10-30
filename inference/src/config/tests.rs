use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

use strict_json::Value;
use team_core::entropy::OsEntropy;

use super::{
    CATALOG_MODELS, ConfigError, InferenceConfig, InferenceConfigStore, KNOWLEDGE_SCHEMA, SCHEMA,
    chat_models, normalize, price,
};
use crate::memory::{Plan, Source};

struct Fixture {
    _directory: tempfile::TempDir,
    root: PathBuf,
    store: InferenceConfigStore,
}

fn fixture() -> Fixture {
    let directory = tempfile::tempdir().expect("tempdir");
    let root = directory.path().join("inference");
    let store = InferenceConfigStore::new(root.clone());
    Fixture {
        _directory: directory,
        root,
        store,
    }
}

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn config(provider: &str, model: &str) -> InferenceConfig {
    normalize(
        &Value::String(provider.to_owned()),
        &Value::String(model.to_owned()),
    )
    .expect("config")
}

fn names(root: &PathBuf) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(root)
        .expect("listed")
        .map(|entry| {
            entry
                .expect("entry")
                .file_name()
                .into_string()
                .expect("name")
        })
        .collect();
    names.sort();
    names
}

fn mode(path: &std::path::Path) -> u32 {
    fs::metadata(path).expect("stat").permissions().mode() & 0o777
}

#[test]
fn only_exact_catalog_chat_values_are_admitted_without_defaults() {
    for (provider, model) in [
        ("null", r#""gpt-6-luna""#),
        (r#""""#, r#""gpt-6-luna""#),
        (r#""OpenAI""#, r#""gpt-6-luna""#),
        (r#"" openai""#, r#""gpt-6-luna""#),
        (r#""openai""#, "null"),
        (r#""openai""#, r#""""#),
        (r#""openai""#, r#""gpt-6-luna ""#),
        (r#""openai""#, "7"),
        (r#""anthropic""#, r#""claude-haiku-5-5""#),
        (r#""unknown-provider""#, r#""gpt-test""#),
        (r#""openai""#, r#""gpt-999""#),
        (r#""openai""#, r#""claude-sonnet-5-5""#),
        (r#""anthropic""#, r#""gpt-6.1-sol""#),
        (r#""openai""#, r#""../../model""#),
    ] {
        assert!(
            normalize(&json(provider), &json(model)).is_err(),
            "{provider} {model}"
        );
    }
    assert_eq!(
        normalize(&json("7"), &json(r#""x""#)),
        Err(ConfigError::UnsupportedProvider)
    );
    assert_eq!(
        normalize(&json(r#""openai""#), &json(r#""x""#)),
        Err(ConfigError::UnsupportedModel)
    );
    for (provider, models) in CATALOG_MODELS.iter() {
        let chat: Vec<&str> = models
            .iter()
            .filter(|model| model.chat)
            .map(|model| model.id.as_str())
            .collect();
        assert_eq!(chat_models(provider), Some(chat.clone()));
        for model in chat {
            assert_eq!(config(provider, model).model, model);
        }
    }
    assert_eq!(
        chat_models("anthropic"),
        Some(vec!["claude-opus-5-5", "claude-sonnet-5-5"])
    );
}

#[test]
fn a_price_is_the_catalog_s_and_an_unknown_model_costs_the_dearest() {
    assert_eq!(price("openai", Some("gpt-6-luna")), Some((10, 50)));
    assert_eq!(price("openai", Some("gpt-999")), Some((200, 1000)));
    assert_eq!(price("anthropic", None), Some((400, 2000)));
    assert_eq!(price("anthropic", Some("claude-haiku-5-5")), Some((10, 50)));
    assert_eq!(price("other", None), None);
}

#[test]
fn save_and_load_preserve_only_provider_and_model() {
    let fixture = fixture();
    let expected = config("anthropic", "claude-sonnet-5-5");
    assert_eq!(
        fixture.store.save("team_1", &expected),
        Ok(expected.clone())
    );
    assert_eq!(fixture.store.load("team_1"), Ok(expected));
    let path = fixture.store.path("team_1", ".json");
    assert_eq!(
        names(&fixture.root),
        vec![
            path.file_name()
                .expect("name")
                .to_string_lossy()
                .into_owned()
        ]
    );
    assert_eq!((mode(&path), mode(&fixture.root)), (0o600, 0o700));
    assert_eq!(
        fs::read(&path).expect("record"),
        format!(r#"{{"model":"claude-sonnet-5-5","provider":"anthropic","schema":{SCHEMA},"team_id":"team_1"}}"#)
            .into_bytes()
    );
}

#[test]
fn team_ids_follow_the_team_protocol_grammar() {
    let fixture = fixture();
    let selection = config("openai", "gpt-6-luna");
    let long = "t".repeat(41);
    for team in ["Team_1", "team-1", long.as_str(), "", "../team"] {
        assert_eq!(
            fixture.store.save(team, &selection),
            Err(ConfigError::InvalidTeam)
        );
        assert_eq!(fixture.store.load(team), Err(ConfigError::InvalidTeam));
        assert_eq!(
            fixture.store.load_knowledge(team),
            Err(ConfigError::InvalidTeam)
        );
        assert_eq!(fixture.store.delete(team), Err(ConfigError::InvalidTeam));
        assert_eq!(
            fixture.store.save_knowledge(team, &Plan::default()),
            Err(ConfigError::InvalidTeam)
        );
    }
    assert!(!fixture.root.exists());
}

#[test]
fn a_replace_is_atomic_and_a_delete_is_idempotent() {
    let fixture = fixture();
    fixture
        .store
        .save("team_1", &config("openai", "gpt-6-luna"))
        .expect("saved");
    fixture
        .store
        .save("team_1", &config("anthropic", "claude-sonnet-5-5"))
        .expect("saved");
    assert_eq!(
        fixture.store.load("team_1").expect("loaded").provider,
        "anthropic"
    );
    assert_eq!(names(&fixture.root).len(), 1);
    fixture.store.delete("team_1").expect("deleted");
    fixture.store.delete("team_1").expect("deleted again");
    assert_eq!(fixture.store.load("team_1"), Err(ConfigError::Missing));
}

#[test]
fn malformed_cross_team_or_outside_catalog_records_are_invalid_never_missing() {
    let fixture = fixture();
    fixture
        .store
        .save("team_1", &config("openai", "gpt-6-luna"))
        .expect("saved");
    let target = fixture.store.path("team_1", ".json");
    let payloads: [&[u8]; 9] = [
        b"\xff",
        b"{",
        b"[]",
        br#"{"schema":3}"#,
        br#"{"schema":4,"team_id":"team_1","provider":"openai","model":"gpt-6.1-sol"}"#,
        br#"{"schema":3,"team_id":"team_2","provider":"openai","model":"gpt-6.1-sol"}"#,
        br#"{"schema":3,"team_id":"team_1","provider":"openai","model":"gpt-999"}"#,
        br#"{"schema":3,"team_id":"team_1","provider":"openai"}"#,
        br#"{"schema":3,"team_id":"team_1","provider":"openai","model":"gpt-6.1-sol","x":0}"#,
    ];
    for payload in payloads {
        fs::write(&target, payload).expect("written");
        assert_eq!(
            fixture.store.load("team_1"),
            Err(ConfigError::Invalid),
            "{payload:?}"
        );
    }
    fs::remove_file(&target).expect("removed");
    fs::create_dir(&target).expect("planted");
    assert_eq!(fixture.store.load("team_1"), Err(ConfigError::Unavailable));
}

#[test]
fn deletions_commit_their_entries_and_an_absent_store_is_already_deleted() {
    let fixture = fixture();
    fixture.store.delete("team_1").expect("absent");
    fixture.store.delete_all().expect("absent");
    assert!(!fixture.root.exists());
    fixture
        .store
        .save("team_1", &config("openai", "gpt-6-luna"))
        .expect("saved");
    fixture
        .store
        .save("team_2", &config("openai", "gpt-6-luna"))
        .expect("saved");
    fs::write(fixture.root.join("foreign.json"), b"{}").expect("foreign");
    fixture.store.delete_all().expect("deleted");
    assert_eq!(names(&fixture.root), vec!["foreign.json"]);
}

fn source() -> Source {
    Source {
        nonce: "b".repeat(32),
        actor_id: "a".repeat(32),
        incarnation: "c".repeat(64),
    }
}

fn apply(store: &InferenceConfigStore, changes: &str, now: i64) -> Plan {
    let plan = store
        .plan_knowledge("team_1", &json(changes), &source(), now, &OsEntropy)
        .expect("planned");
    store.save_knowledge("team_1", &plan).expect("saved");
    plan
}

const LANGUAGE: &str =
    r#"{"op":"remember","topic":"language","preference":"Answer in Brazilian Portuguese."}"#;

#[test]
fn learned_memory_is_stamped_and_a_turn_sees_only_topic_and_preference() {
    let fixture = fixture();
    assert_eq!(fixture.store.load_knowledge("team_1"), Ok(Vec::new()));
    let plan = apply(&fixture.store, &format!("[{LANGUAGE}]"), 4_102_444_800);
    let stored = fixture.store.path("team_1", ".knowledge.json");
    assert_eq!(mode(&stored), 0o600);
    let record = json(&String::from_utf8(fs::read(&stored).expect("record")).expect("utf-8"));
    assert_eq!(
        record.get("schema"),
        Some(&json(&KNOWLEDGE_SCHEMA.to_string()))
    );
    let Some(Value::Array(entries)) = record.get("memory") else {
        panic!("memory");
    };
    assert_eq!(
        entries[0].get("since"),
        Some(&json(r#""2100-01-01T00:00:00Z""#))
    );
    assert_eq!(
        entries[0]
            .get("source")
            .and_then(crate::memory::canonical_source),
        Some(source())
    );
    assert_eq!(
        plan.memory[0].memory_id,
        entries[0]
            .get("memory_id")
            .and_then(team_protocol::python::value::string)
            .expect("id")
    );
    assert_eq!(
        fixture.store.load_knowledge("team_1"),
        Ok(vec![json(
            r#"{"topic":"language","preference":"Answer in Brazilian Portuguese."}"#
        )])
    );
    assert_eq!(fixture.store.load_knowledge("team_2"), Ok(Vec::new()));
    apply(
        &fixture.store,
        r#"[{"op":"forget","topic":"language","preference":""}]"#,
        0,
    );
    assert!(!stored.exists());
}

#[test]
fn invalid_changes_and_sources_are_refused_and_a_failed_write_keeps_the_saved_knowledge() {
    let fixture = fixture();
    let bad = Source {
        incarnation: "network".to_owned(),
        ..source()
    };
    let store = &fixture.store;
    let changes = json(&format!("[{LANGUAGE}]"));
    for (changes, source) in [
        (
            json(r#"[{"op":"remember","topic":"Bad","preference":"x"}]"#),
            source(),
        ),
        (json(r#""not a list""#), source()),
        (changes.clone(), bad),
    ] {
        assert_eq!(
            store
                .plan_knowledge("team_1", &changes, &source, 0, &OsEntropy)
                .err(),
            Some(ConfigError::ChangesInvalid)
        );
    }
    // A record that cannot be replaced (here a directory planted at its name) is reported, and nothing is left.
    let plan = store
        .plan_knowledge("team_1", &changes, &source(), 0, &OsEntropy)
        .expect("planned");
    fs::create_dir_all(store.path("team_1", ".knowledge.json")).expect("planted");
    assert_eq!(
        store.save_knowledge("team_1", &plan),
        Err(ConfigError::KnowledgeNotSaved)
    );
    assert_eq!(names(&fixture.root).len(), 1);
}

#[test]
fn corrupt_or_foreign_knowledge_fails_closed() {
    let fixture = fixture();
    apply(&fixture.store, &format!("[{LANGUAGE}]"), 0);
    let stored = fixture.store.path("team_1", ".knowledge.json");
    let raw = String::from_utf8(fs::read(&stored).expect("record")).expect("utf-8");
    let corrupt = [
        "not json".to_owned(),
        raw.replace("\"team_1\"", "\"team_2\""),
        raw.replace("\"schema\":2", "\"schema\":3"),
        raw.replace("\"memory\":[", "\"extra\":[],\"memory\":["),
        raw.replace("\"topic\":\"language\"", "\"topic\":\"Language\""),
        raw.replace("\"since\":\"1970-01-01T00:00:00Z\"", "\"since\":null"),
        r#"{"memory":[],"schema":2,"team_id":"team_1"}"#.to_owned(),
        r#"["not","an","object"]"#.to_owned(),
    ];
    for payload in corrupt {
        fs::write(&stored, &payload).expect("written");
        assert!(fixture.store.load_knowledge("team_1").is_err(), "{payload}");
    }
    fs::write(&stored, b"\xff").expect("written");
    assert_eq!(
        fixture.store.load_knowledge("team_1"),
        Err(ConfigError::KnowledgeUnavailable)
    );
}
