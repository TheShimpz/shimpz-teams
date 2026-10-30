//! `PDFium`, reached only inside the helper and only for a PDF: the pinned V8- and XFA-free `libpdfium.so` the image
//! carries (`pdfium.toml` records its pins and provenance), bound once per worker process.
//!
//! A document that needs a password, or carries any security handler at all (even one whose user password is empty),
//! is encrypted; any other load failure is unreadable. No form, script, or XFA engine runs: the build has none.

use std::path::Path;

use pdfium_render::prelude::{
    PdfDocument, PdfSecurityHandlerRevision, Pdfium, PdfiumError, PdfiumInternalError,
};

use crate::answer::Reason;

/// Where the Team image installs the pinned library.
pub const LIBRARY: &str = "/usr/local/lib/libpdfium.so";

/// Binds the library at `path`, or reuses the library this process already bound.
///
/// # Errors
/// [`Reason::Unreadable`] when it cannot be loaded.
pub fn bind(path: &Path) -> Result<Pdfium, Reason> {
    match Pdfium::bind_to_library(path) {
        Ok(bindings) => Ok(Pdfium::new(bindings)),
        // Only a process that already bound a library gets here; the default instance reuses that binding.
        Err(PdfiumError::PdfiumLibraryBindingsAlreadyInitialized) => Ok(Pdfium::default()),
        Err(_) => Err(Reason::Unreadable),
    }
}

/// Opens `data` without a password, refusing any encrypted document.
///
/// # Errors
/// [`Reason::Encrypted`] for a document with a security handler, else [`Reason::Unreadable`] when it does not open.
pub fn open<'a>(pdfium: &'a Pdfium, data: &'a [u8]) -> Result<PdfDocument<'a>, Reason> {
    let document = pdfium
        .load_pdf_from_byte_slice(data, None)
        .map_err(|error| match error {
            PdfiumError::PdfiumLibraryInternalError(PdfiumInternalError::PasswordError) => {
                Reason::Encrypted
            }
            _ => Reason::Unreadable,
        })?;
    match document.permissions().security_handler_revision() {
        Ok(PdfSecurityHandlerRevision::Unprotected) => Ok(document),
        _ => Err(Reason::Encrypted),
    }
}
