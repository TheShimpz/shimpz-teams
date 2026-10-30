//! The bodies of one Team's diagnostic directory, by the name each kind is written under:
//!
//! - an attempt's diagnostic: `{at}.{routine}.{run}.{operation}.{attempt}.diagnostic`;
//! - a step record: `{at}.{routine}.{run}.{sequence}.step`;
//! - a run's terminal record: `{at}.{routine}.{run}.run`.
//!
//! `at` is 1 to 12 decimal digits, `routine` and `run` 32 lowercase hex digits, `operation` a version-4 UUID, `attempt`
//! 1 or 2 digits, and `sequence` 1 to 5 digits. Any other name is not a body and is left alone.

use std::path::Path;

use super::{
    DiagnosticError, MAX_PLAINTEXT_BYTES, MAX_RUN_PLAINTEXT_BYTES, MAX_STEP_PLAINTEXT_BYTES,
};

/// A body's kind.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// An attempt's diagnostic.
    Diagnostic,
    /// A step record.
    Step,
    /// A run's terminal record.
    Run,
}

impl Kind {
    /// The kind's plaintext bound.
    pub const fn bound(self) -> usize {
        match self {
            Self::Diagnostic => MAX_PLAINTEXT_BYTES,
            Self::Step => MAX_STEP_PLAINTEXT_BYTES,
            Self::Run => MAX_RUN_PLAINTEXT_BYTES,
        }
    }
}

/// One body: its name, kind, the parts its name carries, and its size.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// The file name.
    pub name: String,
    /// The kind.
    pub kind: Kind,
    /// When it was recorded.
    pub at: i64,
    /// The Routine.
    pub routine: String,
    /// The run.
    pub run: String,
    /// A diagnostic's logical operation.
    pub operation: Option<String>,
    /// A diagnostic's attempt or a step record's sequence, as written.
    pub number: Option<String>,
    /// Its size in bytes.
    pub size: u64,
}

fn digits(text: &str, low: usize, high: usize) -> bool {
    (low..=high).contains(&text.len()) && text.bytes().all(|byte| byte.is_ascii_digit())
}

fn hex(text: &str, length: usize) -> bool {
    text.len() == length
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// `[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}`.
fn uuid4(text: &str) -> bool {
    let groups: Vec<&str> = text.split('-').collect();
    groups.len() == 5
        && hex(groups[0], 8)
        && hex(groups[1], 4)
        && hex(groups[2], 4)
        && groups[2].starts_with('4')
        && hex(groups[3], 4)
        && groups[3].starts_with(['8', '9', 'a', 'b'])
        && hex(groups[4], 12)
}

/// The kind and parts of a body name, or `None` for a name that is no body.
pub fn parse(name: &str, size: u64) -> Option<Entry> {
    let parts: Vec<&str> = name.split('.').collect();
    let (kind, operation, number) = match parts.as_slice() {
        [_, _, _, operation, attempt, "diagnostic"]
            if uuid4(operation) && digits(attempt, 1, 2) =>
        {
            (
                Kind::Diagnostic,
                Some((*operation).to_owned()),
                Some((*attempt).to_owned()),
            )
        }
        [_, _, _, sequence, "step"] if digits(sequence, 1, 5) => {
            (Kind::Step, None, Some((*sequence).to_owned()))
        }
        [_, _, _, "run"] => (Kind::Run, None, None),
        _ => return None,
    };
    let (at, routine, run) = (parts[0], parts[1], parts[2]);
    if !digits(at, 1, 12) || !hex(routine, 32) || !hex(run, 32) {
        return None;
    }
    Some(Entry {
        name: name.to_owned(),
        kind,
        at: at.parse().ok()?,
        routine: routine.to_owned(),
        run: run.to_owned(),
        operation,
        number,
        size,
    })
}

/// The Team's bodies of every kind, oldest first (then by name); a non-file where one belongs fails closed and an
/// absent directory holds none.
///
/// # Errors
/// [`DiagnosticError::Unsafe`] for a body that is not a regular file, [`DiagnosticError::Unavailable`] when the
/// directory cannot be listed.
pub fn entries(directory: &Path) -> Result<Vec<Entry>, DiagnosticError> {
    let listed = match std::fs::read_dir(directory) {
        Ok(listed) => listed,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err(DiagnosticError::Unavailable),
    };
    let mut found = Vec::new();
    for entry in listed {
        let entry = entry.map_err(|_| DiagnosticError::Unavailable)?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if parse(&name, 0).is_none() {
            continue;
        }
        let metadata =
            std::fs::symlink_metadata(entry.path()).map_err(|_| DiagnosticError::Unavailable)?;
        if !metadata.file_type().is_file() {
            return Err(DiagnosticError::Unsafe);
        }
        found.extend(parse(&name, metadata.len()));
    }
    found.sort_by(|left, right| (left.at, &left.name).cmp(&(right.at, &right.name)));
    Ok(found)
}
