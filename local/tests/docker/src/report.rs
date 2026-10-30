//! Check accounting: every check that holds prints one `ok` line, and the first that fails ends the suite with its
//! evidence. The suite passes only when its last line is `ALL <n> CHECKS PASS`.

use std::fmt::Debug;

/// Why the suite stopped: a check that failed, or a step that could not be carried out.
#[derive(Debug)]
pub struct Failure(pub String);

impl std::fmt::Display for Failure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl From<std::io::Error> for Failure {
    fn from(error: std::io::Error) -> Self {
        Self(format!("I/O failed: {error}"))
    }
}

/// The outcome of one step.
pub type Outcome<T = ()> = Result<T, Failure>;

/// A failure carrying `message`.
pub fn fail<T>(message: impl Into<String>) -> Outcome<T> {
    Err(Failure(message.into()))
}

/// An expected value beside the one found.
struct Mismatch<T> {
    expected: T,
    actual: T,
}

impl<T: Debug> Debug for Mismatch<T> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "expected {:?}, found {:?}",
            self.expected, self.actual
        )
    }
}

/// The strict JSON document `text`.
pub fn document(text: &str) -> Outcome<strict_json::Value> {
    strict_json::parse(text.as_bytes()).map_err(|_| Failure(format!("not JSON: {text}")))
}

/// The checks that held so far.
#[derive(Debug, Default)]
pub struct Report {
    passed: usize,
}

impl Report {
    /// Records `description` as holding, or fails with `detail`.
    pub fn check(&mut self, description: &str, holds: bool, detail: impl Debug) -> Outcome {
        if !holds {
            return fail(format!("{description}: {detail:?}"));
        }
        drop(detail);
        self.passed += 1;
        println!("ok {} - {description}", self.passed);
        Ok(())
    }

    /// Records `description` as holding when `actual` equals `expected`.
    pub fn equal<T: PartialEq + Debug>(
        &mut self,
        description: &str,
        actual: T,
        expected: T,
    ) -> Outcome {
        let holds = actual == expected;
        self.check(description, holds, Mismatch { expected, actual })
    }

    /// Records `description` as holding when `actual` equals the JSON `expected` as Python compares values (object
    /// member order aside).
    pub fn same(
        &mut self,
        description: &str,
        actual: &strict_json::Value,
        expected: &str,
    ) -> Outcome {
        let expected = document(expected)?;
        if team_protocol::python::value::py_eq(actual, &expected) {
            self.check(description, true, "")
        } else {
            let compact = |value: &strict_json::Value| {
                team_protocol::python::json::dumps(value, team_protocol::python::json::CANONICAL)
            };
            fail(format!(
                "{description}: expected {}, found {}",
                compact(&expected),
                compact(actual)
            ))
        }
    }

    /// How many checks held.
    pub const fn passed(&self) -> usize {
        self.passed
    }
}

#[cfg(test)]
mod tests {
    use super::Report;

    #[test]
    fn checks_count_until_the_first_failure() {
        let mut report = Report::default();
        assert!(report.equal("same", 1, 1).is_ok());
        assert!(report.check("holds", true, "unused").is_ok());
        let failure = report.equal("differs", "a", "b").expect_err("a failure");
        assert_eq!(failure.0, "differs: expected \"b\", found \"a\"");
        assert_eq!(report.passed(), 2);
    }
}
