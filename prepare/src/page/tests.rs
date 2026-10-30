use image::{GenericImageView, ImageReader};

use super::{MAX_PAGE_SCALE, bitmap_size, prepare};
use crate::answer::{MediaType, Reason};
use crate::fixtures::{self, pdfium_library};
use crate::limits::{DERIVATIVE_LONG_EDGE, DERIVATIVE_PIXELS};
use crate::pdfium::bind;

fn page(data: &[u8], number: i64) -> Result<(crate::answer::Derivative, i64), Reason> {
    prepare(
        data,
        number,
        &bind(&pdfium_library()).expect("the pinned PDFium binds"),
    )
}

#[test]
fn each_page_becomes_a_bounded_derivative_with_the_page_count() {
    let document = fixtures::text_pdf(&["First page", "Second page"]);
    for number in [1, 2] {
        let (derivative, count) = page(&document, number).expect("rendered");
        assert_eq!(count, 2);
        assert_eq!(derivative.media_type, MediaType::Png);
        assert_eq!((derivative.width, derivative.height), (962, 1246));
        let decoded = ImageReader::new(std::io::Cursor::new(&derivative.bytes))
            .with_guessed_format()
            .expect("readable")
            .decode()
            .expect("a PNG");
        assert_eq!(decoded.dimensions(), (962, 1246));
        // White paper with dark text drawn on it.
        assert_eq!(decoded.get_pixel(0, 0).0, [255, 255, 255, 255]);
        assert!(decoded.pixels().any(|(_, _, pixel)| pixel.0[0] < 128));
    }
}

#[test]
fn encrypted_oversized_unreadable_and_absent_pages_have_no_rendition() {
    let document = fixtures::text_pdf(&["Only page"]);
    for (data, number, reason) in [
        (fixtures::ENCRYPTED_PDF.to_vec(), 1, Reason::Encrypted),
        (
            fixtures::OPENLY_ENCRYPTED_PDF.to_vec(),
            1,
            Reason::Encrypted,
        ),
        (fixtures::blank_pdf(51), 1, Reason::TooLarge),
        (fixtures::FLATE_BOMB_PDF.to_vec(), 1, Reason::TooLarge),
        (b"%PDF-1.4\n garbage".to_vec(), 1, Reason::Unreadable),
        (document, 2, Reason::Unsupported),
    ] {
        assert_eq!(
            page(&data, number).map(|(_, count)| count),
            Err(reason),
            "{reason:?}"
        );
    }
}

#[test]
fn the_bitmap_is_sized_before_it_is_drawn_within_the_derivative() {
    let fits = |(width, height): (u32, u32)| {
        let (width, height) = (width as usize, height as usize);
        width.max(height) <= DERIVATIVE_LONG_EDGE && width * height <= DERIVATIVE_PIXELS
    };
    for (width, height) in [(612.0, 792.0), (14_400.0, 10.0), (3.0, 3.0), (1e6, 1e6)] {
        let size = bitmap_size(width, height).expect("a size");
        assert!(fits(size), "{size:?}");
    }
    // A small page is drawn larger, at most four times.
    assert_eq!(bitmap_size(10.0, 20.0), Ok((40, 80)));
    assert!((MAX_PAGE_SCALE - 4.0).abs() < f64::EPSILON);
    for (width, height) in [
        (0.0, 10.0),
        (-1.0, 10.0),
        (f64::NAN, 1.0),
        (f64::INFINITY, 1.0),
    ] {
        assert_eq!(bitmap_size(width, height), Err(Reason::Unreadable));
    }
}
