use super::{prepare, prepass};
use crate::answer::Reason;
use crate::fixtures::{self, pdfium_library};
use crate::limits::MAX_PDF_PAGES;
use crate::pdfium::bind;

fn text(data: &[u8]) -> Result<String, Reason> {
    prepare(
        data,
        &bind(&pdfium_library()).expect("the pinned PDFium binds"),
    )
}

fn pages(count: i64) -> usize {
    usize::try_from(count).expect("a page count")
}

#[test]
fn pdf_text_is_extracted_page_by_page() {
    assert_eq!(
        text(&fixtures::text_pdf(&["Invoice 42", "Total 1000"])).as_deref(),
        Ok("Invoice 42\n\nTotal 1000")
    );
    assert_eq!(
        text(&fixtures::text_pdf(&["Hello from the helper"])).as_deref(),
        Ok("Hello from the helper")
    );
    // Long text within the ceiling comes out whole; the same lines on more pages exceed it.
    let long = text(&fixtures::lined_pdf(40, 700)).expect("within the ceiling");
    assert_eq!(long.matches('x').count(), 40 * 700);
}

#[test]
fn encrypted_image_only_oversized_and_malformed_pdfs_are_refused() {
    for (data, reason) in [
        (fixtures::ENCRYPTED_PDF.to_vec(), Reason::Encrypted),
        (fixtures::OPENLY_ENCRYPTED_PDF.to_vec(), Reason::Encrypted),
        (fixtures::blank_pdf(2), Reason::NoText),
        (fixtures::pageless_pdf(), Reason::NoText),
        (
            fixtures::blank_pdf(pages(MAX_PDF_PAGES) + 1),
            Reason::TooLarge,
        ),
        (
            b"%PDF-1.4\n garbage without objects".to_vec(),
            Reason::Unreadable,
        ),
        (fixtures::lined_pdf(49, 700), Reason::TooLarge),
        (fixtures::FLATE_BOMB_PDF.to_vec(), Reason::TooLarge),
    ] {
        assert_eq!(text(&data), Err(reason), "{reason:?}");
    }
}

#[test]
fn the_pre_pass_bounds_pages_and_streams_before_pdfium_reads_anything() {
    assert_eq!(prepass(&fixtures::text_pdf(&["a", "b"])), Ok(2));
    assert_eq!(prepass(fixtures::FLATE_BOMB_PDF), Err(Reason::TooLarge));
    assert_eq!(prepass(fixtures::ENCRYPTED_PDF), Err(Reason::Encrypted));
    assert_eq!(
        prepass(fixtures::OPENLY_ENCRYPTED_PDF),
        Err(Reason::Encrypted)
    );
    assert_eq!(
        prepass(&fixtures::blank_pdf(pages(MAX_PDF_PAGES) + 1)),
        Err(Reason::TooLarge)
    );
    // Nested far beyond any parser's recursion: refused, never a crash.
    let nested = [
        &b"%PDF-1.4\n1 0 obj "[..],
        &b"[".repeat(100_000),
        b" endobj\n",
    ]
    .concat();
    assert_eq!(prepass(&nested), Err(Reason::Unreadable));
}

#[test]
fn a_script_or_form_engine_never_runs_and_only_the_text_comes_out() {
    assert_eq!(text(fixtures::SCRIPTED_PDF).as_deref(), Ok("Scripted page"));
}
