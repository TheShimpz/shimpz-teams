//! Team attachment preparation (ADR-0093; ADR-0123, plan phase R7, workstream W4b). It replaces `prepare/`.
//!
//! Two sides share these rules. The worker the `shimpz-team prepare` subcommand runs, once per file inside the
//! networkless helper container, turns one framed original into one closed answer: an image decoded within pixel
//! limits, oriented, resized, and re-encoded without metadata ([`image`]); a PDF's text after a bounded pre-pass
//! ([`pdf`]); or one PDF page drawn by `PDFium` ([`page`]). The controller detects each file's kind from its bytes
//! ([`detect`]), reads text itself, and re-validates every helper answer as untrusted before it reaches the Brain or a
//! Supervisor's browser ([`content`], [`message`]).

pub mod answer;
pub mod content;
pub mod detect;
#[cfg(any(test, feature = "testing"))]
pub mod fixtures;
pub mod image;
pub mod limits;
pub mod message;
pub mod page;
pub mod pdf;
pub mod pdfium;
pub mod request;
pub mod worker;
