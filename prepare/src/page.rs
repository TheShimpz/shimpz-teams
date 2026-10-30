//! Render one page of a PDF into a bounded image; runs only inside the preparation helper (ADR-0093).
//!
//! After the same pre-pass as a PDF's text, `PDFium` draws the page on white into a bitmap whose size is fixed here
//! before it is allocated: the page's own proportions within the derivative's long edge and pixel count. Only the
//! page's pixels leave, re-encoded fresh as an image's derivative is, preferring a PNG (a page is mostly text and flat
//! color, which a PNG keeps sharp).

use pdfium_render::prelude::{PdfBitmapFormat, PdfColor, PdfRenderConfig, Pdfium};

use crate::answer::{Derivative, Reason};
use crate::image::{self, Fresh, edge, floored, pixels};
use crate::limits::MAX_PDF_PAGES;
use crate::pdf::prepass;
use crate::pdfium;

/// A page smaller than the derivative is drawn larger, up to this factor: a PDF page is vector, so the larger drawing
/// stays sharp, while a page a few points wide never becomes a bitmap of mostly scaling.
pub const MAX_PAGE_SCALE: f64 = 4.0;

/// The pixel size one page of `width` by `height` points is drawn at.
///
/// # Errors
/// [`Reason::Unreadable`] for a page without a finite positive size.
pub fn bitmap_size(width: f64, height: f64) -> Result<(u32, u32), Reason> {
    if !(width.is_finite() && height.is_finite() && width > 0.0 && height > 0.0) {
        return Err(Reason::Unreadable);
    }
    let scale = MAX_PAGE_SCALE
        .min(edge() / width.max(height))
        .min((pixels() / (width * height)).sqrt());
    // Both sides round down, so the bitmap never exceeds the derivative's long edge or pixel count.
    Ok((floored(width * scale), floored(height * scale)))
}

/// Page `page_number` (from 1) as a bounded derivative, with the document's page count.
///
/// # Errors
/// The closed [`Reason`] the page is refused for: [`Reason::Unsupported`] for a page beyond the document's last.
pub fn prepare(
    data: &[u8],
    page_number: i64,
    pdfium: &Pdfium,
) -> Result<(Derivative, i64), Reason> {
    prepass(data)?;
    let document = pdfium::open(pdfium, data)?;
    let pages = document.pages();
    let page_count = i64::from(pages.len());
    if page_count > MAX_PDF_PAGES {
        return Err(Reason::TooLarge);
    }
    if page_number > page_count {
        return Err(Reason::Unsupported);
    }
    let index = i32::try_from(page_number - 1).map_err(|_| Reason::Unsupported)?;
    let page = pages.get(index).map_err(|_| Reason::Unreadable)?;
    let (width, height) = bitmap_size(
        f64::from(page.width().value),
        f64::from(page.height().value),
    )?;
    let (pixel_width, pixel_height) = (
        i32::try_from(width).map_err(|_| Reason::Unreadable)?,
        i32::try_from(height).map_err(|_| Reason::Unreadable)?,
    );
    let config = PdfRenderConfig::new()
        .set_fixed_width(pixel_width)
        .set_fixed_height(pixel_height)
        .set_format(PdfBitmapFormat::BGRA)
        .set_clear_color(PdfColor::WHITE)
        .render_form_data(false)
        .render_annotations(true);
    let bitmap = page
        .render_with_config(&config)
        .map_err(|_| Reason::Unreadable)?;
    let drawn =
        image::rgb_from_rgba(width, height, bitmap.as_rgba_bytes()).ok_or(Reason::Unreadable)?;
    Ok((image::encode(Fresh::Rgb(drawn), true)?, page_count))
}

#[cfg(test)]
mod tests;
