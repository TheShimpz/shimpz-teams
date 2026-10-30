//! The preparation helper's fixed entrypoint: one bounded file in, one closed answer out (ADR-0093).
//!
//! `shimpz-team prepare` runs it as a fresh process per file inside the networkless, read-only, non-root helper
//! container. It first confines itself (5 CPU seconds, 64 descriptors, no core file, no written file), reads one framed
//! request from standard input, and writes exactly one compact ASCII JSON answer to standard output. Anything else on
//! standard output, anything on standard error, a non-zero exit, or a timeout is a refused file for the controller. A
//! decoder failure on hostile input refuses only its one file: a panic is caught silently and answers `unreadable`.

use std::io::{self, Read, Write};
use std::panic::{self, AssertUnwindSafe};
use std::path::Path;
use std::process::ExitCode;

use rustix::process::{Resource, Rlimit, setrlimit};

use crate::answer::{Answer, Reason};
use crate::limits::{HELPER_CPU_SECONDS, HELPER_OPEN_FILES, MAX_HELPER_INPUT_BYTES};
use crate::request::{self, Kind};
use crate::{image, page, pdf, pdfium};

/// The answer to one framed request; `library` is the `PDFium` library a PDF binds.
#[must_use]
pub fn answer(raw: &[u8], library: &Path) -> Answer {
    let Some(request) = request::decode(raw) else {
        return Answer::Opaque(Reason::Unreadable);
    };
    let prepared = panic::catch_unwind(AssertUnwindSafe(|| match request.kind {
        Kind::Image => image::prepare(request.data).map(Answer::Image),
        Kind::Pdf => pdfium::bind(library)
            .and_then(|bound| pdf::prepare(request.data, &bound).map(Answer::Text)),
        Kind::PdfPage => pdfium::bind(library).and_then(|bound| {
            page::prepare(request.data, request.page.unwrap_or_default(), &bound)
                .map(|(derivative, count)| Answer::Page(derivative, count))
        }),
    }));
    match prepared {
        Ok(Ok(answer)) => answer,
        Ok(Err(reason)) => Answer::Opaque(reason),
        Err(_) => Answer::Opaque(Reason::Unreadable),
    }
}

/// Reads one request from `input` (at most one byte beyond the frame ceiling) and writes its answer to `output`.
///
/// # Errors
/// The I/O error that kept the request from being read or the answer from being written.
pub fn run(input: impl Read, mut output: impl Write, library: &Path) -> io::Result<()> {
    let mut raw = Vec::new();
    input
        .take(u64::try_from(MAX_HELPER_INPUT_BYTES + 1).unwrap_or(u64::MAX))
        .read_to_end(&mut raw)?;
    let answer = if raw.len() > MAX_HELPER_INPUT_BYTES {
        Answer::Opaque(Reason::Unreadable)
    } else {
        answer(&raw, library)
    };
    output.write_all(answer.to_json().as_bytes())?;
    output.flush()
}

/// Bounds this process to its per-file CPU budget, few descriptors, no core file, and no written file.
///
/// # Errors
/// The error of a limit that could not be set; the worker then answers nothing.
pub fn confine() -> io::Result<()> {
    let both = |limit: u64| Rlimit {
        current: Some(limit),
        maximum: Some(limit),
    };
    setrlimit(Resource::Cpu, both(HELPER_CPU_SECONDS))?;
    setrlimit(Resource::Nofile, both(HELPER_OPEN_FILES))?;
    setrlimit(Resource::Core, both(0))?;
    setrlimit(Resource::Fsize, both(0))?;
    Ok(())
}

/// The `shimpz-team prepare` subcommand: [`serve`] with the image's pinned library.
#[must_use]
pub fn main() -> ExitCode {
    serve(Path::new(pdfium::LIBRARY))
}

/// Confines this process, then answers standard input on standard output; a PDF binds `library`.
///
/// Library diagnostics stay silent (no logger is installed and a panic prints nothing), because standard error is a
/// failed file for the controller. Exit status 0 means one answer was written.
#[must_use]
pub fn serve(library: &Path) -> ExitCode {
    panic::set_hook(Box::new(|_| {}));
    if confine().is_err() {
        return ExitCode::from(71);
    }
    match run(io::stdin().lock(), io::stdout().lock(), library) {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => ExitCode::from(74),
    }
}

#[cfg(test)]
mod tests;
