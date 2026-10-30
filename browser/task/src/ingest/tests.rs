use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::stop::Stop;

use super::{
    DownloadSink, Ingested, ListingError, MAX_FILE_BYTES, MAX_FILES, MAX_LISTED, PutRefused,
    ReadLost, ingest, present,
};
use crate::testing::json;

const PDF: &[u8] = b"%PDF-1.4\n";

fn listing(files: &[(&str, u64)], refused: &str, skipped: u64) -> Value {
    let files: Vec<String> = files
        .iter()
        .map(|(name, size)| format!(r#"{{"name": "{name}", "size": {size}}}"#))
        .collect();
    json(&format!(
        r#"{{"files": [{}], "refused": {refused}, "skipped": {skipped}}}"#,
        files.join(", ")
    ))
}

/// A store that reads from `contents`, fails as `errors` says, and records each read and store.
#[derive(Default)]
struct Recorder {
    contents: HashMap<String, Vec<u8>>,
    unreadable: Vec<String>,
    refusals: HashMap<String, PutRefused>,
    events: Mutex<Vec<(&'static str, String)>>,
    /// Set when a file is read, for a Stop that arrives during the read.
    stop_on_read: Option<Stop>,
}

impl Recorder {
    fn with(contents: &[(&str, &[u8])]) -> Self {
        Self {
            contents: contents
                .iter()
                .map(|(name, data)| ((*name).to_owned(), data.to_vec()))
                .collect(),
            ..Self::default()
        }
    }

    fn events(&self) -> MutexGuard<'_, Vec<(&'static str, String)>> {
        self.events.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl DownloadSink for Recorder {
    fn read<'a>(&'a self, name: &'a str, _size: u64) -> BoxFuture<'a, Result<Vec<u8>, ReadLost>> {
        Box::pin(async move {
            self.events().push(("read", name.to_owned()));
            if let Some(stop) = &self.stop_on_read {
                stop.stop();
            }
            if self.unreadable.iter().any(|unreadable| unreadable == name) {
                return Err(ReadLost);
            }
            self.contents.get(name).cloned().ok_or(ReadLost)
        })
    }

    fn put<'a>(&'a self, name: &'a str, data: Vec<u8>) -> BoxFuture<'a, Result<Value, PutRefused>> {
        Box::pin(async move {
            self.events().push(("put", name.to_owned()));
            if let Some(refusal) = self.refusals.get(name) {
                return Err(*refusal);
            }
            Ok(json(&format!(
                r#"{{"id": "id-{name}", "name": "{name}", "media_type": "application/pdf", "size": {}, "sha256": "x"}}"#,
                data.len()
            )))
        })
    }
}

fn names(ingested: &Ingested) -> Vec<String> {
    ingested
        .kept
        .iter()
        .map(|kept| match kept.get("name") {
            Some(Value::String(name)) => name.clone(),
            _ => String::new(),
        })
        .collect()
}

fn lost(entries: &[(&str, &'static str)]) -> Vec<(String, &'static str)> {
    entries
        .iter()
        .map(|(name, reason)| ((*name).to_owned(), *reason))
        .collect()
}

#[tokio::test]
async fn each_kept_file_is_read_exactly_then_stored_and_kept_by_its_reference() {
    let recorder = Recorder::with(&[("edital.pdf", PDF)]);
    let ingested = ingest(
        &listing(&[("edital.pdf", 9)], "[]", 0),
        &recorder,
        &Stop::new(),
    )
    .await
    .expect("listed");
    assert_eq!(
        ingested.kept,
        [json(
            r#"{"id": "id-edital.pdf", "name": "edital.pdf", "media_type": "application/pdf", "size": 9}"#
        )]
    );
    assert_eq!((ingested.lost.len(), ingested.skipped), (0, 0));
    assert_eq!(
        *recorder.events(),
        [
            ("read", "edital.pdf".to_owned()),
            ("put", "edital.pdf".to_owned())
        ]
    );
}

#[tokio::test]
async fn every_file_not_kept_has_its_closed_reason() {
    let files: Vec<String> = (0..MAX_FILES)
        .map(|index| format!("f{index}.bin"))
        .collect();
    let mut recorder = Recorder::with(&[]);
    recorder.contents = files.iter().map(|name| (name.clone(), vec![0])).collect();
    recorder.refusals = [
        ("f1.bin".to_owned(), PutRefused::QuotaExceeded),
        ("f2.bin".to_owned(), PutRefused::NameRefused),
        ("f3.bin".to_owned(), PutRefused::Unavailable),
    ]
    .into();
    let mut listed: Vec<(&str, u64)> = files.iter().map(|name| (name.as_str(), 1)).collect();
    listed.extend([
        ("extra.bin", 1),
        ("huge.iso", MAX_FILE_BYTES + 1),
        ("empty.txt", 0),
    ]);
    let refused = r#"[{"name": "Unconfirmed 1.crdownload", "reason": "incomplete", "size": 7}]"#;
    let ingested = ingest(&listing(&listed, refused, 2), &recorder, &Stop::new())
        .await
        .expect("listed");
    assert_eq!(ingested.kept.len(), MAX_FILES - 3);
    assert_eq!(
        ingested.lost,
        lost(&[
            ("Unconfirmed 1.crdownload", "incomplete"),
            ("f1.bin", "quota-exceeded"),
            ("f2.bin", "name-refused"),
            ("f3.bin", "store-unavailable"),
            ("extra.bin", "over-limit"),
            ("huge.iso", "over-limit"),
            ("empty.txt", "over-limit"),
        ])
    );
    assert_eq!(ingested.skipped, 2);
}

#[tokio::test]
async fn an_empty_download_is_not_kept() {
    let recorder = Recorder::default();
    let ingested = ingest(
        &listing(&[("empty.txt", 0)], "[]", 0),
        &recorder,
        &Stop::new(),
    )
    .await
    .expect("listed");
    assert_eq!(
        (ingested.kept.len(), ingested.lost),
        (0, lost(&[("empty.txt", "empty")]))
    );
    assert_eq!(recorder.events().len(), 0);
}

#[tokio::test]
async fn a_lost_read_ends_the_ingest_and_names_every_later_file() {
    let mut recorder = Recorder::with(&[("a.pdf", PDF)]);
    recorder.unreadable.push("b.pdf".to_owned());
    let ingested = ingest(
        &listing(&[("a.pdf", 9), ("b.pdf", 9), ("c.pdf", 9)], "[]", 0),
        &recorder,
        &Stop::new(),
    )
    .await
    .expect("listed");
    assert_eq!(names(&ingested), ["a.pdf"]);
    assert_eq!(
        ingested.lost,
        lost(&[("b.pdf", "browser-lost"), ("c.pdf", "browser-lost")])
    );
}

/// A store that sets the Stop once it stored a file.
struct StopAfterPut(Recorder, Stop);
impl DownloadSink for StopAfterPut {
    fn read<'a>(&'a self, name: &'a str, size: u64) -> BoxFuture<'a, Result<Vec<u8>, ReadLost>> {
        self.0.read(name, size)
    }

    fn put<'a>(&'a self, name: &'a str, data: Vec<u8>) -> BoxFuture<'a, Result<Value, PutRefused>> {
        Box::pin(async move {
            let stored = self.0.put(name, data).await;
            self.1.stop();
            stored
        })
    }
}

#[tokio::test]
async fn a_stop_ends_the_ingest_before_the_next_file() {
    let stop = Stop::new();
    let stopping = stop.clone();
    let sink = StopAfterPut(Recorder::with(&[("a.pdf", PDF), ("b.pdf", PDF)]), stopping);
    let ingested = ingest(
        &listing(&[("a.pdf", 9), ("b.pdf", 9)], "[]", 0),
        &sink,
        &stop,
    )
    .await
    .expect("listed");
    assert_eq!(names(&ingested), ["a.pdf"]);
    assert!(!sink.0.events().contains(&("read", "b.pdf".to_owned())));
}

#[tokio::test]
async fn a_stop_during_a_read_stores_nothing() {
    let stop = Stop::new();
    let mut recorder = Recorder::with(&[("a.pdf", PDF)]);
    recorder.stop_on_read = Some(stop.clone());
    let ingested = ingest(&listing(&[("a.pdf", 9)], "[]", 0), &recorder, &stop)
        .await
        .expect("listed");
    assert_eq!(ingested.kept.len(), 0);
    assert_eq!(*recorder.events(), [("read", "a.pdf".to_owned())]);
}

#[tokio::test]
async fn a_store_refused_for_stop_ends_the_ingest() {
    let mut recorder = Recorder::with(&[("a.pdf", PDF), ("b.pdf", PDF)]);
    recorder
        .refusals
        .insert("a.pdf".to_owned(), PutRefused::Stopped);
    let ingested = ingest(
        &listing(&[("a.pdf", 9), ("b.pdf", 9)], "[]", 0),
        &recorder,
        &Stop::new(),
    )
    .await
    .expect("listed");
    assert_eq!((ingested.kept.len(), ingested.lost.len()), (0, 0));
    assert!(!recorder.events().contains(&("read", "b.pdf".to_owned())));
}

#[tokio::test]
async fn a_store_past_the_tasks_deadline_loses_that_file_and_every_later_one() {
    let mut recorder = Recorder::with(&[("a.pdf", PDF), ("b.pdf", PDF)]);
    recorder
        .refusals
        .insert("a.pdf".to_owned(), PutRefused::ReadLost);
    let ingested = ingest(
        &listing(&[("a.pdf", 9), ("b.pdf", 9)], "[]", 0),
        &recorder,
        &Stop::new(),
    )
    .await
    .expect("listed");
    assert_eq!(ingested.kept.len(), 0);
    assert_eq!(
        ingested.lost,
        lost(&[("a.pdf", "browser-lost"), ("b.pdf", "browser-lost")])
    );
}

#[tokio::test]
async fn a_malformed_listing_is_refused_whole() {
    let too_many: Vec<String> = (0..=MAX_LISTED)
        .map(|_| r#"{"name": "a", "size": 1}"#.to_owned())
        .collect();
    let too_many = format!(
        r#"{{"files": [{}], "refused": [], "skipped": 0}}"#,
        too_many.join(",")
    );
    for broken in [
        "{}",
        r#"{"files": [], "refused": [], "skipped": -1}"#,
        r#"{"files": [], "refused": [], "skipped": true}"#,
        r#"{"files": "x", "refused": [], "skipped": 0}"#,
        r#"{"files": [{"name": "a"}], "refused": [], "skipped": 0}"#,
        r#"{"files": [{"name": "", "size": 1}], "refused": [], "skipped": 0}"#,
        r#"{"files": [{"name": "a", "size": -1}], "refused": [], "skipped": 0}"#,
        r#"{"files": [{"name": "a", "size": 1.0}], "refused": [], "skipped": 0}"#,
        r#"{"files": [{"name": "a", "size": 1, "x": 1}], "refused": [], "skipped": 0}"#,
        r#"{"files": ["a"], "refused": [], "skipped": 0}"#,
        r#"{"files": [], "refused": [{"name": "a", "reason": "other", "size": 1}], "skipped": 0}"#,
        r#"{"files": [], "refused": [{"name": "", "reason": "incomplete", "size": 1}], "skipped": 0}"#,
        r#"{"files": [], "refused": ["a"], "skipped": 0}"#,
        &too_many,
    ] {
        let recorder = Recorder::default();
        assert_eq!(
            ingest(&json(broken), &recorder, &Stop::new()).await,
            Err(ListingError)
        );
        assert_eq!(recorder.events().len(), 0);
    }
}

fn present_listing(files: &[&str], refused: &[(&str, &str)], skipped: u64) -> Value {
    let files: Vec<(&str, u64)> = files.iter().map(|name| (*name, 1)).collect();
    let refused: Vec<String> = refused
        .iter()
        .map(|(name, reason)| format!(r#"{{"name": "{name}", "reason": "{reason}", "size": 1}}"#))
        .collect();
    listing(&files, &format!("[{}]", refused.join(", ")), skipped)
}

#[test]
fn a_step_sees_completed_and_over_limit_files_and_counts_the_ones_still_downloading() {
    let listed = present_listing(
        &["b.csv", "a.csv"],
        &[("c.crdownload", "incomplete"), ("big.zip", "over-limit")],
        0,
    );
    assert_eq!(
        present(&listed),
        Ok(json(
            r#"{"files": ["a.csv", "b.csv", "big.zip"], "pending": 1, "truncated": false}"#
        ))
    );
}

#[test]
fn names_left_out_mark_the_list_truncated() {
    let many: Vec<String> = (0..25).map(|index| format!("f{index:02}.csv")).collect();
    let many: Vec<&str> = many.iter().map(String::as_str).collect();
    let shown = present(&present_listing(&many, &[], 0)).expect("listed");
    let Some(Value::Array(files)) = shown.get("files") else {
        panic!("files are listed");
    };
    assert_eq!(
        (files.len(), shown.get("truncated")),
        (20, Some(&Value::Bool(true)))
    );
    let odd = present(&present_listing(&["ok.csv", "bad\\nname.csv"], &[], 0)).expect("listed");
    assert_eq!(
        odd,
        json(r#"{"files": ["ok.csv"], "pending": 0, "truncated": true}"#)
    );
    let skipped = present(&present_listing(&[], &[], 2)).expect("listed");
    assert_eq!(skipped.get("truncated"), Some(&Value::Bool(true)));
    assert_eq!(
        present(&json(r#"{"files": "x", "refused": [], "skipped": 0}"#)),
        Err(ListingError)
    );
}
