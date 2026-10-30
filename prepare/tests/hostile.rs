//! The hostile corpus, each file answered by a fresh worker process confined as the helper's is (5 CPU seconds, 64
//! descriptors, no core file, no written file): every answer is one closed JSON object on standard output, nothing
//! reaches standard error, and the process exits 0.
//!
//! The test binary is its own worker: started with `SHIMPZ_PREPARE_WORKER` set, it runs the worker's entrypoint with
//! the pinned `PDFium` that `SHIMPZ_PDFIUM_LIBRARY` names, exactly as `shimpz-team prepare` does with the image's.

#[path = "../src/fixtures.rs"]
#[expect(
    dead_code,
    reason = "the builders are shared with the unit tests; this corpus uses some"
)]
mod fixtures;

use std::io::Write;
use std::path::Path;
use std::process::{Command, ExitCode, Stdio};

use strict_json::Value;
use team_prepare::limits::{MAX_HELPER_INPUT_BYTES, MAX_PDF_PAGES};
use team_prepare::request::{Kind, encode};
use team_prepare::worker;

const CHILD: &str = "SHIMPZ_PREPARE_WORKER";

/// One hostile file and what its answer must be: an opaque reason, or a readable `type`.
struct Case {
    name: &'static str,
    request: Vec<u8>,
    expected: (&'static str, &'static str),
}

fn opaque(
    name: &'static str,
    kind: Kind,
    data: &[u8],
    page: Option<i64>,
    reason: &'static str,
) -> Case {
    Case {
        name,
        request: encode(kind, data, page),
        expected: ("opaque", reason),
    }
}

fn readable(
    name: &'static str,
    kind: Kind,
    data: &[u8],
    page: Option<i64>,
    kind_name: &'static str,
) -> Case {
    Case {
        name,
        request: encode(kind, data, page),
        expected: (kind_name, ""),
    }
}

fn images() -> Vec<Case> {
    let png = fixtures::image(fixtures::Format::Png, 8, 8, false);
    let zeros = [&b"\x89PNG\r\n\x1a\n"[..], &[0; 64]].concat();
    vec![
        opaque(
            "pixel bomb png",
            Kind::Image,
            fixtures::PIXEL_BOMB_PNG,
            None,
            "too_large",
        ),
        opaque(
            "pixel bomb jpeg",
            Kind::Image,
            fixtures::PIXEL_BOMB_JPEG,
            None,
            "too_large",
        ),
        opaque(
            "animated webp",
            Kind::Image,
            fixtures::ANIMATED_WEBP,
            None,
            "animated",
        ),
        opaque("gif", Kind::Image, fixtures::GIF, None, "unsupported"),
        opaque("truncated png", Kind::Image, &png[..40], None, "unreadable"),
        opaque(
            "png signature over zeros",
            Kind::Image,
            &zeros,
            None,
            "unreadable",
        ),
        opaque(
            "pdf as image",
            Kind::Image,
            &fixtures::text_pdf(&["x"]),
            None,
            "unreadable",
        ),
        readable("ordinary png", Kind::Image, &png, None, "image"),
    ]
}

fn encrypted_pdfs() -> Vec<Case> {
    let (owner, open) = (fixtures::ENCRYPTED_PDF, fixtures::OPENLY_ENCRYPTED_PDF);
    vec![
        opaque("encrypted pdf", Kind::Pdf, owner, None, "encrypted"),
        opaque("openly encrypted", Kind::Pdf, open, None, "encrypted"),
        opaque("encrypted page", Kind::PdfPage, owner, Some(1), "encrypted"),
    ]
}

fn oversized_pdfs() -> Vec<Case> {
    let (bomb, pages) = (
        fixtures::FLATE_BOMB_PDF,
        usize::try_from(MAX_PDF_PAGES).unwrap_or_default(),
    );
    vec![
        opaque("flate bomb pdf", Kind::Pdf, bomb, None, "too_large"),
        opaque("flate bomb page", Kind::PdfPage, bomb, Some(1), "too_large"),
        opaque(
            "too many pages",
            Kind::Pdf,
            &fixtures::blank_pdf(pages + 1),
            None,
            "too_large",
        ),
    ]
}

fn other_pdfs() -> Vec<Case> {
    let nested = [
        &b"%PDF-1.4\n1 0 obj "[..],
        &b"[".repeat(200_000),
        b" endobj\n",
    ]
    .concat();
    let (garbage, scripted) = (
        b"%PDF-1.4\n garbage without objects",
        fixtures::SCRIPTED_PDF,
    );
    let one = fixtures::text_pdf(&["x"]);
    vec![
        opaque(
            "image-only pdf",
            Kind::Pdf,
            &fixtures::blank_pdf(2),
            None,
            "no_text",
        ),
        opaque("garbage pdf", Kind::Pdf, garbage, None, "unreadable"),
        opaque("nested pdf", Kind::Pdf, &nested, None, "unreadable"),
        opaque("absent page", Kind::PdfPage, &one, Some(2), "unsupported"),
        readable("scripted pdf", Kind::Pdf, scripted, None, "text"),
        readable("scripted page", Kind::PdfPage, scripted, Some(1), "image"),
    ]
}

fn frames() -> Vec<Case> {
    vec![
        Case {
            name: "misframed request",
            request: b"{\"kind\":\"image\",\"size\":9}\nx".to_vec(),
            expected: ("opaque", "unreadable"),
        },
        Case {
            name: "oversized request",
            request: vec![b'x'; MAX_HELPER_INPUT_BYTES + 1],
            expected: ("opaque", "unreadable"),
        },
    ]
}

fn field<'a>(answer: &'a Value, name: &str) -> &'a str {
    match answer.get(name) {
        Some(Value::String(text)) => text,
        _ => "",
    }
}

fn check(case: &Case, library: &Path) -> Result<(), String> {
    let mut child = Command::new(std::env::current_exe().map_err(|error| error.to_string())?)
        .env_clear()
        .env(CHILD, "1")
        .env("SHIMPZ_PDFIUM_LIBRARY", library)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    let mut stdin = child.stdin.take().ok_or("no stdin")?;
    let request = case.request.clone();
    let writer = std::thread::spawn(move || {
        let _ = stdin.write_all(&request);
    });
    let output = child
        .wait_with_output()
        .map_err(|error| error.to_string())?;
    let _ = writer.join();
    if !output.status.success() || !output.stderr.is_empty() {
        return Err(format!(
            "exit {:?}, stderr {:?}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    if !output.stdout.is_ascii() {
        return Err("a non-ASCII answer".to_owned());
    }
    let answer =
        strict_json::parse(&output.stdout).map_err(|_| "not one JSON document".to_owned())?;
    let (kind, reason) = case.expected;
    let found = (
        field(&answer, "type"),
        if kind == "opaque" {
            field(&answer, "reason")
        } else {
            ""
        },
    );
    if found == (kind, reason) {
        Ok(())
    } else {
        Err(format!("answered {found:?}"))
    }
}

/// The limits a confined worker runs under, as the child reports them after confining itself.
fn confined_limits() -> Result<(), String> {
    let output = Command::new(std::env::current_exe().map_err(|error| error.to_string())?)
        .env_clear()
        .env(CHILD, "limits")
        .output()
        .map_err(|error| error.to_string())?;
    let reported = String::from_utf8_lossy(&output.stdout);
    if output.status.success() && reported == "cpu=5/5 nofile=64/64 core=0/0 fsize=0/0" {
        Ok(())
    } else {
        Err(format!("confined to {reported:?}"))
    }
}

fn report_limits() -> ExitCode {
    use rustix::process::{Resource, getrlimit};
    if worker::confine().is_err() {
        return ExitCode::FAILURE;
    }
    let limit = |resource| {
        let found = getrlimit(resource);
        format!(
            "{}/{}",
            found.current.unwrap_or(u64::MAX),
            found.maximum.unwrap_or(u64::MAX)
        )
    };
    print!(
        "cpu={} nofile={} core={} fsize={}",
        limit(Resource::Cpu),
        limit(Resource::Nofile),
        limit(Resource::Core),
        limit(Resource::Fsize)
    );
    ExitCode::SUCCESS
}

fn main() -> ExitCode {
    if std::env::var_os(CHILD).is_some_and(|mode| mode == "limits") {
        return report_limits();
    }
    if std::env::var_os(CHILD).is_some() {
        let library = std::env::var_os("SHIMPZ_PDFIUM_LIBRARY").unwrap_or_default();
        return worker::serve(Path::new(&library));
    }
    // As libtest does, `--ignored` runs only the ignored cases, and the hostile corpus has none.
    if std::env::args()
        .skip(1)
        .any(|argument| argument == "--ignored")
    {
        return ExitCode::SUCCESS;
    }
    let library = fixtures::pdfium_library();
    let mut failed = 0_u8;
    match confined_limits() {
        Ok(()) => println!("hostile confinement ... ok"),
        Err(error) => {
            println!("hostile confinement ... FAILED: {error}");
            failed = 1;
        }
    }
    let corpus = [
        images(),
        encrypted_pdfs(),
        oversized_pdfs(),
        other_pdfs(),
        frames(),
    ];
    for case in corpus.into_iter().flatten() {
        match check(&case, &library) {
            Ok(()) => println!("hostile {} ... ok", case.name),
            Err(error) => {
                println!("hostile {} ... FAILED: {error}", case.name);
                failed = failed.saturating_add(1);
            }
        }
    }
    if failed == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
