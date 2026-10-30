//! Crash points for the crash-injection tests.
//!
//! With the `failpoints` feature, `HOLDPOINT_FAILPOINT=name` aborts the process the first time execution reaches
//! `name`, and `HOLDPOINT_FAILPOINT=name@N` the N-th time. Without the feature every point compiles to nothing.
//! Coverage cannot see it: a hit aborts the child process before its profile is written.

/// Aborts the process when the configured failpoint is `name`.
#[cfg(feature = "failpoints")]
#[cfg_attr(coverage_nightly, coverage(off))]
pub(crate) fn hit(name: &str) {
    use std::sync::Mutex;
    static HITS: Mutex<Vec<(String, u32)>> = Mutex::new(Vec::new());
    let Ok(selected) = std::env::var("HOLDPOINT_FAILPOINT") else {
        return;
    };
    let (point, at) =
        selected.split_once('@').map_or((selected.as_str(), 1), |(point, count)| (point, count.parse().unwrap_or(1)));
    if point != name {
        return;
    }
    let mut hits = HITS.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    let count = if let Some((_, count)) = hits.iter_mut().find(|(known, _)| known == name) {
        *count += 1;
        *count
    } else {
        hits.push((name.to_owned(), 1));
        1
    };
    if count >= at {
        std::process::abort();
    }
}

/// Without the `failpoints` feature a crash point does nothing.
#[cfg(not(feature = "failpoints"))]
#[inline]
pub(crate) fn hit(_name: &str) {}
