//! The Docker CLI, run with a bounded wall time; the suite drives the daemon only through it.

use std::io::{Read, Write};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use crate::report::{Outcome, fail};

/// How long one Docker command may run (an image build or push included).
const DEADLINE: Duration = Duration::from_mins(10);

/// One finished Docker command.
#[derive(Debug)]
pub struct Output {
    /// Whether it exited 0.
    pub success: bool,
    /// Its standard output.
    pub stdout: String,
    /// Its standard error.
    pub stderr: String,
}

impl Output {
    /// The standard output without its surrounding whitespace.
    pub fn text(&self) -> &str {
        self.stdout.trim()
    }
}

fn collect(mut stream: impl Read + Send + 'static) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        // A read error leaves what was read; the exit status still decides the outcome.
        let _ = stream.read_to_end(&mut bytes);
        bytes
    })
}

fn finish(mut child: Child, arguments: &[&str]) -> Outcome<Output> {
    let stdout = collect(child.stdout.take().ok_or_else(|| missing(arguments))?);
    let stderr = collect(child.stderr.take().ok_or_else(|| missing(arguments))?);
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if started.elapsed() > DEADLINE {
            let _ = child.kill();
            let _ = child.wait();
            return fail(format!(
                "docker {} outlived {DEADLINE:?}",
                arguments.join(" ")
            ));
        }
        thread::sleep(Duration::from_millis(10));
    };
    Ok(Output {
        success: status.success(),
        stdout: String::from_utf8_lossy(&stdout.join().unwrap_or_default()).into_owned(),
        stderr: String::from_utf8_lossy(&stderr.join().unwrap_or_default()).into_owned(),
    })
}

fn missing(arguments: &[&str]) -> crate::report::Failure {
    crate::report::Failure(format!("docker {} lost a stream", arguments.join(" ")))
}

/// Runs `docker ARGUMENTS`, with `input` on its standard input, and reports how it ended.
pub fn run_with(arguments: &[&str], input: Option<&[u8]>) -> Outcome<Output> {
    let mut child = Command::new("docker")
        .args(arguments)
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    if let Some(input) = input {
        let mut stdin = child.stdin.take().ok_or_else(|| missing(arguments))?;
        stdin.write_all(input)?;
    }
    finish(child, arguments)
}

/// Runs `docker ARGUMENTS` and reports how it ended.
pub fn run(arguments: &[&str]) -> Outcome<Output> {
    run_with(arguments, None)
}

/// Runs `docker ARGUMENTS`, which must succeed, and returns its trimmed standard output.
pub fn ok(arguments: &[&str]) -> Outcome<String> {
    let output = run(arguments)?;
    if output.success {
        Ok(output.text().to_owned())
    } else {
        let tail: String = output
            .stderr
            .chars()
            .rev()
            .take(2000)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        fail(format!("docker {} failed: {tail}", arguments.join(" ")))
    }
}

/// Whether `docker ARGUMENTS` succeeds; a failure to run Docker at all is a failure of the suite.
pub fn succeeds(arguments: &[&str]) -> Outcome<bool> {
    Ok(run(arguments)?.success)
}

/// Runs a cleanup command; its outcome is proved by the checks that follow, not here.
pub fn quietly(arguments: &[&str]) {
    let _ = run(arguments);
}

/// Docker's own answer that `reference` names no image.
pub fn image_absent(reference: &str) -> Outcome<bool> {
    Ok(!succeeds(&["image", "inspect", reference])?)
}

/// The JSON inspection of one object.
pub fn inspect(arguments: &[&str]) -> Outcome<strict_json::Value> {
    let raw = ok(arguments)?;
    let document = strict_json::parse(raw.as_bytes()).map_err(|_| {
        crate::report::Failure(format!("docker {} answered no JSON", arguments.join(" ")))
    })?;
    match document {
        strict_json::Value::Array(mut items) if items.len() == 1 => Ok(items.remove(0)),
        _ => fail(format!(
            "docker {} answered no single object",
            arguments.join(" ")
        )),
    }
}

/// Runs `docker ARGUMENTS`, hands its standard output to `consume` as it streams, and requires both to succeed.
pub fn stream<T>(
    arguments: &[&str],
    consume: impl FnOnce(&mut dyn Read) -> Outcome<T>,
) -> Outcome<T> {
    let mut child = Command::new("docker")
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let mut stdout = child.stdout.take().ok_or_else(|| missing(arguments))?;
    let stderr = collect(child.stderr.take().ok_or_else(|| missing(arguments))?);
    let consumed = consume(&mut stdout);
    // Whatever `consume` left unread is drained, so the command can finish.
    let _ = std::io::copy(&mut stdout, &mut std::io::sink());
    let status = child.wait()?;
    let stderr = String::from_utf8_lossy(&stderr.join().unwrap_or_default()).into_owned();
    if status.success() {
        consumed
    } else {
        fail(format!(
            "docker {} failed: {}",
            arguments.join(" "),
            stderr.trim()
        ))
    }
}

/// A Docker command line built from fixed words and run-specific values.
#[derive(Clone, Debug, Default)]
pub struct Args(Vec<String>);

impl Args {
    /// Appends each whitespace-separated word of `text`.
    #[must_use]
    pub fn words(mut self, text: &str) -> Self {
        self.0.extend(text.split_whitespace().map(str::to_owned));
        self
    }

    /// Appends `value` as one argument.
    #[must_use]
    pub fn arg(mut self, value: impl Into<String>) -> Self {
        self.0.push(value.into());
        self
    }

    /// Appends `flag` and `value`.
    #[must_use]
    pub fn flag(self, flag: &str, value: impl Into<String>) -> Self {
        self.arg(flag).arg(value)
    }

    /// The arguments.
    pub fn borrowed(&self) -> Vec<&str> {
        self.0.iter().map(String::as_str).collect()
    }

    /// Runs the command, which must succeed, and returns its trimmed standard output.
    pub fn ok(&self) -> Outcome<String> {
        ok(&self.borrowed())
    }
}
