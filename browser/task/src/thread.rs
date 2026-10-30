//! A Browser task's thread: what each Brain call of the task carries (ADR-0117, 2026-10-22 and 2026-10-23 amendments).
//!
//! Team keeps it in memory for the task only: each unfolded step's observation, the downloads and screenshot it saw,
//! the admitted step, and its outcome; the folded steps' records; and the agent's task memory. Entries are appended as
//! outcomes arrive and never rewritten, so the conversation the Brain renders from consecutive requests only grows at
//! its end. When an entry would push the history past its bound, the oldest unfolded steps fold (their pages, read
//! records, and screenshots discarded) and the agent rewrites its whole memory, which replaces the one before (a fold
//! whose rewrite failed keeps the old memory and counts its steps as unnoted); the oldest folded lines go first when
//! even that does not fit, counted.

use strict_json::{Number, Value};
use team_protocol::browser::{
    MAX_FOLDED_BYTES, MAX_HISTORY_BYTES, MAX_MEMORY_BYTES, entry_bytes, history_bytes, record_bytes,
};

/// A fold leaves the history at most this large with the incoming entry, so the next one comes only after a few steps.
pub const FOLD_TARGET_BYTES: usize = MAX_HISTORY_BYTES / 2;

/// One task's thread. Entries are canonical step entries (`step`, `outcome`, `page`, `text`, `downloads`,
/// `screenshot`); folded records keep their `step`, `outcome`, and `page`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Thread {
    /// The folded steps' records, oldest first.
    pub folded: Vec<Value>,
    /// How many folded records were dropped to keep the folded lines within their bound.
    pub folded_omitted: u64,
    /// The agent's whole task memory.
    pub memory: String,
    /// How many folded steps left without their memory being rewritten.
    pub unnoted: u64,
    /// The unfolded steps, oldest first.
    pub steps: Vec<Value>,
}

fn count(value: u64) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

impl Thread {
    /// The thread as a request carries it.
    #[must_use]
    pub fn wire(&self) -> Value {
        Value::Object(vec![
            ("folded".to_owned(), Value::Array(self.folded.clone())),
            ("folded_omitted".to_owned(), count(self.folded_omitted)),
            ("memory".to_owned(), Value::String(self.memory.clone())),
            ("unnoted".to_owned(), count(self.unnoted)),
            ("steps".to_owned(), Value::Array(self.steps.clone())),
        ])
    }

    /// The 1-based step number of the unfolded step at `position`.
    #[must_use]
    pub fn number(&self, position: usize) -> u64 {
        self.folded_omitted + (self.folded.len() + position + 1) as u64
    }

    /// At most what the thread's history renders to.
    #[must_use]
    pub fn history_bytes(&self) -> usize {
        history_bytes(&self.wire())
    }

    /// How many of the oldest unfolded steps fold before an entry of `incoming` bytes joins: none while it fits.
    ///
    /// The count assumes the rewritten memory takes its whole bound. Folding aims at [`FOLD_TARGET_BYTES`], and only
    /// steps fold, so a large incoming entry may fold them all; [`MAX_HISTORY_BYTES`] is the bound.
    #[must_use]
    pub fn to_fold(&self, incoming: usize) -> usize {
        let history = self.history_bytes();
        if history + incoming <= MAX_HISTORY_BYTES {
            return 0;
        }
        let mut size = history - self.memory.len() + MAX_MEMORY_BYTES + incoming;
        let mut folded = 0;
        for entry in &self.steps {
            if size <= FOLD_TARGET_BYTES {
                break;
            }
            size = size.saturating_sub(entry_bytes(entry) - record_bytes(&record(entry)));
            folded += 1;
        }
        folded
    }

    /// Moves the oldest `count` unfolded steps into folded records; `memory` replaces the agent's memory.
    ///
    /// Without a new memory, the old one stays and the steps whose pages left unnoted are counted.
    pub fn fold(&mut self, count: usize, memory: Option<String>) {
        let count = count.min(self.steps.len());
        let moved: Vec<Value> = self.steps.drain(..count).collect();
        self.folded.extend(moved.iter().map(record));
        match memory {
            Some(memory) => self.memory = memory,
            None => self.unnoted += count as u64,
        }
        while self.folded.iter().map(record_bytes).sum::<usize>() > MAX_FOLDED_BYTES {
            self.folded.remove(0);
            self.folded_omitted += 1;
        }
    }

    /// Adds a step as its outcome arrives, dropping the oldest folded lines while it does not fit.
    pub fn append(&mut self, entry: Value) {
        self.steps.push(entry);
        while self.history_bytes() > MAX_HISTORY_BYTES && !self.folded.is_empty() {
            self.folded.remove(0);
            self.folded_omitted += 1;
        }
    }
}

/// A step entry's folded record: its step, outcome, and page.
fn record(entry: &Value) -> Value {
    Value::Object(
        ["step", "outcome", "page"]
            .into_iter()
            .map(|key| {
                (
                    key.to_owned(),
                    entry.get(key).cloned().unwrap_or(Value::Null),
                )
            })
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use strict_json::Value;
    use team_protocol::browser::{
        MAX_FOLDED_BYTES, MAX_HISTORY_BYTES, MAX_MEMORY_BYTES, MAX_OBSERVATION_BYTES,
        canonical_thread, entry_bytes, record_bytes,
    };
    use team_protocol::python::value::object_of;

    use super::Thread;

    fn entry(text: &str, note: Option<&str>) -> Value {
        let mut step = vec![("kind", Value::String("wait".to_owned()))];
        if let Some(note) = note {
            step.push(("note", Value::String(note.to_owned())));
        }
        object_of(vec![
            ("step", object_of(step)),
            ("outcome", Value::String("ok".to_owned())),
            (
                "page",
                object_of(vec![
                    ("url", Value::String("https://example.com".to_owned())),
                    ("title", Value::String("Example".to_owned())),
                ]),
            ),
            ("text", Value::String(text.to_owned())),
            ("downloads", Value::Null),
            ("screenshot", Value::Bool(false)),
        ])
    }

    fn record(note: &str) -> Value {
        super::record(&entry("", Some(note)))
    }

    fn full_folded() -> (Thread, usize) {
        let lines = MAX_FOLDED_BYTES / record_bytes(&record(&"n".repeat(300)));
        let thread = Thread {
            folded: vec![record(&"n".repeat(300)); lines],
            memory: "m".repeat(MAX_MEMORY_BYTES),
            ..Thread::default()
        };
        (thread, lines)
    }

    #[test]
    fn a_fold_stops_once_the_history_reaches_its_target() {
        let thread = Thread {
            steps: vec![entry(&"x".repeat(12_000), None); 10],
            ..Thread::default()
        };
        let count = thread.to_fold(9_600);
        assert!(count > 0);
        assert!(count < thread.steps.len());
    }

    #[test]
    fn full_pages_fold_only_every_few_steps_with_a_full_memory() {
        let (mut thread, _lines) = full_folded();
        let mut folds = 0;
        for _ in 0..30 {
            let entry = entry(&"x".repeat(MAX_OBSERVATION_BYTES), None);
            let count = thread.to_fold(entry_bytes(&entry));
            if count > 0 {
                folds += 1;
                thread.fold(count, Some("m".repeat(MAX_MEMORY_BYTES)));
            }
            thread.append(entry);
            assert!(canonical_thread(&thread.wire()).is_some());
        }
        assert!(folds <= 30 / 3);
    }

    #[test]
    fn a_fold_replaces_the_memory_with_the_rewritten_one() {
        let mut thread = Thread {
            memory: "V101: R$ 9.500".to_owned(),
            steps: vec![entry("x", None), entry("y", None)],
            ..Thread::default()
        };
        thread.fold(1, Some("V101: R$ 9.500\nV102: R$ 7.000".to_owned()));
        assert_eq!(thread.memory, "V101: R$ 9.500\nV102: R$ 7.000");
        assert_eq!(thread.folded.len(), 1);
    }

    #[test]
    fn a_fold_without_a_memory_keeps_the_old_one_and_counts_its_steps_as_unnoted() {
        let full = "m".repeat(MAX_MEMORY_BYTES);
        let mut thread = Thread {
            memory: full.clone(),
            steps: vec![entry("x", None), entry("y", None), entry("z", None)],
            ..Thread::default()
        };
        thread.fold(2, None);
        assert_eq!((thread.memory.as_str(), thread.unnoted), (full.as_str(), 2));
        thread.fold(1, Some("V101: R$ 9.500".to_owned()));
        assert_eq!(
            (thread.memory.as_str(), thread.unnoted),
            ("V101: R$ 9.500", 2)
        );
        assert_eq!(canonical_thread(&thread.wire()), Some(thread.wire()));
    }

    #[test]
    fn an_append_past_the_bound_drops_the_oldest_folded_lines() {
        let (mut thread, lines) = full_folded();
        let room = MAX_HISTORY_BYTES - thread.history_bytes() - entry_bytes(&entry("", None));
        thread.append(entry(&"x".repeat(room - 1_000), None));
        thread.append(entry(&"y".repeat(1_000), None));
        assert!(thread.history_bytes() <= MAX_HISTORY_BYTES);
        assert!(thread.folded_omitted > 0);
        assert_eq!(
            thread.folded.len() as u64 + thread.folded_omitted,
            lines as u64
        );
    }

    #[test]
    fn a_fold_keeps_the_folded_lines_within_their_bound() {
        let mut thread = Thread {
            folded: vec![record(&"n".repeat(300)); 20],
            steps: vec![entry("x", None)],
            ..Thread::default()
        };
        thread.fold(1, Some("c".repeat(8_000)));
        assert!(thread.folded.iter().map(record_bytes).sum::<usize>() <= MAX_FOLDED_BYTES);
        assert!(thread.folded_omitted > 0);
    }
}
