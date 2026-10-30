use image::codecs::jpeg::JpegDecoder;
use image::{ImageDecoder, Rgb, RgbImage, Rgba, RgbaImage};

use super::{Fresh, encode, prepare, scaled};
use crate::answer::{Derivative, MediaType, Reason};
use crate::fixtures::{self, Format};
use crate::limits::{
    DERIVATIVE_LONG_EDGE, DERIVATIVE_PIXELS, MAX_DERIVATIVE_BYTES, MAX_IMAGE_EDGE,
};

/// Deterministic noise, which no encoder compresses well.
fn noise(width: u32, height: u32) -> impl FnMut() -> u8 {
    let mut state = u64::from(width) * 31 + u64::from(height);
    move || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        state.to_be_bytes()[0]
    }
}

fn within_derivative(derivative: &Derivative) {
    let (width, height) = (derivative.width as usize, derivative.height as usize);
    assert!(width.max(height) <= DERIVATIVE_LONG_EDGE);
    assert!(width * height <= DERIVATIVE_PIXELS);
    assert!(derivative.bytes.len() <= MAX_DERIVATIVE_BYTES);
    assert!(
        derivative
            .bytes
            .starts_with(derivative.media_type.signature())
    );
}

#[test]
fn a_large_photo_becomes_a_bounded_metadata_free_jpeg() {
    let derivative = prepare(&fixtures::oriented_jpeg(3600, 2700, 1)).expect("prepared");
    within_derivative(&derivative);
    assert_eq!(derivative.media_type, MediaType::Jpeg);
    assert_eq!((derivative.width, derivative.height), (1264, 948));
    let mut decoder = JpegDecoder::new(std::io::Cursor::new(&derivative.bytes)).expect("a JPEG");
    assert_eq!(decoder.dimensions(), (1264, 948));
    assert_eq!(decoder.icc_profile().expect("read"), None);
    assert_eq!(decoder.exif_metadata().expect("read"), None);
    for private in [&b"Exif\0\0"[..], b"ICC_PROFILE", b"Private Camera Maker"] {
        assert!(
            !derivative
                .bytes
                .windows(private.len())
                .any(|window| window == private)
        );
    }
}

#[test]
fn orientation_is_applied_before_the_metadata_is_dropped() {
    let derivative = prepare(&fixtures::oriented_jpeg(300, 100, 6)).expect("prepared");
    assert_eq!((derivative.width, derivative.height), (100, 300));
}

#[test]
fn transparency_keeps_a_png_and_webp_is_read() {
    let png = prepare(&fixtures::image(Format::Png, 64, 32, true)).expect("prepared");
    assert_eq!(
        (png.media_type, png.width, png.height),
        (MediaType::Png, 64, 32)
    );
    let webp = prepare(&fixtures::image(Format::Webp, 64, 32, false)).expect("prepared");
    assert_eq!(
        (webp.media_type, webp.width, webp.height),
        (MediaType::Jpeg, 64, 32)
    );
}

#[test]
fn hostile_or_unsupported_images_are_refused_with_a_closed_reason() {
    let png = fixtures::image(Format::Png, 8, 8, false);
    for (data, reason) in [
        (
            fixtures::image(Format::Png, 4000, 4000, false),
            Reason::TooLarge,
        ),
        (
            fixtures::image(Format::Png, MAX_IMAGE_EDGE + 1, 2, false),
            Reason::TooLarge,
        ),
        (fixtures::PIXEL_BOMB_PNG.to_vec(), Reason::TooLarge),
        (fixtures::PIXEL_BOMB_JPEG.to_vec(), Reason::TooLarge),
        (fixtures::ANIMATED_WEBP.to_vec(), Reason::Animated),
        (fixtures::GIF.to_vec(), Reason::Unsupported),
        (png[..40].to_vec(), Reason::Unreadable),
        (
            [&b"\x89PNG\r\n\x1a\n"[..], &[0; 64]].concat(),
            Reason::Unreadable,
        ),
        (
            b"neither an image nor anything else".to_vec(),
            Reason::Unreadable,
        ),
    ] {
        assert_eq!(prepare(&data), Err(reason), "{reason:?}");
    }
}

#[test]
fn an_oversized_transparent_png_falls_back_to_jpeg_and_nothing_fitting_is_refused() {
    let mut next = noise(600, 600);
    let transparent = RgbaImage::from_fn(600, 600, |_, _| Rgba([next(), next(), next(), next()]));
    let derivative = encode(Fresh::Rgba(transparent), false).expect("a JPEG fits");
    assert_eq!(derivative.media_type, MediaType::Jpeg);
    within_derivative(&derivative);
    let mut next = noise(1568, 765);
    let opaque = RgbImage::from_fn(1568, 765, |_, _| Rgb([next(), next(), next()]));
    assert_eq!(encode(Fresh::Rgb(opaque), true), Err(Reason::TooLarge));
}

#[test]
fn the_derivative_size_stays_within_both_ceilings_and_one_pixel() {
    assert_eq!(scaled(3600, 2700), (1264, 948));
    assert_eq!(scaled(10_000, 3), (1568, 1));
    assert_eq!(scaled(20, 10), (20, 10));
    assert_eq!(scaled(1568, 1568), (1095, 1095));
}
