//! Decode one image and re-encode fresh pixels without metadata; runs only inside the preparation helper.
//!
//! Only a JPEG, PNG, or static WebP is decoded, after its header proves it within the pixel and edge ceilings. Its EXIF
//! orientation is applied, its pixels are copied into a new buffer (so no EXIF, XMP, ICC, or text chunk can follow
//! them), scaled down with Lanczos3 within the derivative's long edge and pixel count, and encoded as a PNG when it has
//! transparency and fits, else as a JPEG at the first quality from 85 down to 45 that fits.

use std::io::Cursor;

use image::codecs::jpeg::{JpegDecoder, JpegEncoder};
use image::codecs::png::{CompressionType, FilterType as PngFilter, PngDecoder, PngEncoder};
use image::codecs::webp::WebPDecoder;
use image::imageops::FilterType;
use image::{
    DynamicImage, ExtendedColorType, ImageDecoder, ImageEncoder, ImageError, Limits, Pixel, Rgb,
    RgbImage, RgbaImage,
};

use crate::answer::{Derivative, MediaType, Reason};
use crate::limits::{
    DERIVATIVE_LONG_EDGE, DERIVATIVE_PIXELS, MAX_DERIVATIVE_BYTES, MAX_IMAGE_EDGE, MAX_IMAGE_PIXELS,
};

const JPEG_QUALITIES: [u8; 5] = [85, 75, 65, 55, 45];
// What one decode may allocate: a 16-bit RGBA image at the pixel ceiling, within the helper's 256 MiB.
const MAX_DECODE_ALLOCATION: u64 = 96 * 1024 * 1024;

/// Fresh decoded pixels, with or without an alpha channel.
#[derive(Clone, Debug)]
pub enum Fresh {
    /// Opaque pixels.
    Rgb(RgbImage),
    /// Pixels with transparency.
    Rgba(RgbaImage),
}

/// One bounded JPEG or PNG derivative of a JPEG, PNG, or static WebP original.
///
/// # Errors
/// The closed [`Reason`] the image is refused for.
pub fn prepare(data: &[u8]) -> Result<Derivative, Reason> {
    encode(fresh(&decode(data)?), false)
}

fn limits() -> Limits {
    let mut limits = Limits::default();
    limits.max_image_width = Some(MAX_IMAGE_EDGE);
    limits.max_image_height = Some(MAX_IMAGE_EDGE);
    limits.max_alloc = Some(MAX_DECODE_ALLOCATION);
    limits
}

fn refused(error: &ImageError) -> Reason {
    match error {
        ImageError::Limits(_) => Reason::TooLarge,
        _ => Reason::Unreadable,
    }
}

/// Decodes the original after its header admits it, with its orientation applied.
fn decode(data: &[u8]) -> Result<DynamicImage, Reason> {
    let reader = Cursor::new(data);
    if data.starts_with(MediaType::Jpeg.signature()) {
        oriented(JpegDecoder::new(reader).map_err(|error| refused(&error))?)
    } else if data.starts_with(MediaType::Png.signature()) {
        let decoder = PngDecoder::with_limits(reader, limits()).map_err(|error| refused(&error))?;
        if decoder.is_apng().map_err(|error| refused(&error))? {
            return Err(Reason::Animated);
        }
        oriented(decoder)
    } else if data.len() >= 12 && data.starts_with(b"RIFF") && &data[8..12] == b"WEBP" {
        let decoder = WebPDecoder::new(reader).map_err(|error| refused(&error))?;
        if decoder.has_animation() {
            return Err(Reason::Animated);
        }
        oriented(decoder)
    } else if image::guess_format(data).is_ok() {
        // A known image format outside the closed v1 rules (GIF, BMP, TIFF, …).
        Err(Reason::Unsupported)
    } else {
        Err(Reason::Unreadable)
    }
}

fn oriented(mut decoder: impl ImageDecoder) -> Result<DynamicImage, Reason> {
    let (width, height) = decoder.dimensions();
    if width.max(height) > MAX_IMAGE_EDGE || u64::from(width) * u64::from(height) > MAX_IMAGE_PIXELS
    {
        return Err(Reason::TooLarge);
    }
    decoder
        .set_limits(limits())
        .map_err(|error| refused(&error))?;
    let orientation = decoder.orientation().map_err(|error| refused(&error))?;
    let mut image = DynamicImage::from_decoder(decoder).map_err(|error| refused(&error))?;
    image.apply_orientation(orientation);
    Ok(image)
}

/// The decoded pixels copied into a new buffer, scaled down within the derivative's long edge and pixel count.
fn fresh(decoded: &DynamicImage) -> Fresh {
    let (width, height) = (decoded.width(), decoded.height());
    let (target_width, target_height) = scaled(width, height);
    let resized = |image: DynamicImage| {
        if (target_width, target_height) == (width, height) {
            image
        } else {
            image.resize_exact(target_width, target_height, FilterType::Lanczos3)
        }
    };
    if decoded.color().has_alpha() {
        Fresh::Rgba(resized(DynamicImage::ImageRgba8(decoded.to_rgba8())).to_rgba8())
    } else {
        Fresh::Rgb(resized(DynamicImage::ImageRgb8(decoded.to_rgb8())).to_rgb8())
    }
}

/// The size an image of `width` by `height` is scaled down to: both sides round down, never below one pixel.
#[must_use]
pub fn scaled(width: u32, height: u32) -> (u32, u32) {
    let (w, h) = (f64::from(width), f64::from(height));
    let scale = 1.0_f64
        .min(edge() / w.max(h))
        .min((pixels() / (w * h)).sqrt());
    (floored(w * scale), floored(h * scale))
}

/// The derivative's long edge as a float.
pub(crate) fn edge() -> f64 {
    f64::from(u32::try_from(DERIVATIVE_LONG_EDGE).unwrap_or(u32::MAX))
}

/// The derivative's pixel count as a float.
pub(crate) fn pixels() -> f64 {
    f64::from(u32::try_from(DERIVATIVE_PIXELS).unwrap_or(u32::MAX))
}

/// `max(1, floor(value))` for a value at most the derivative's long edge.
pub(crate) fn floored(value: f64) -> u32 {
    let longest = u32::try_from(DERIVATIVE_LONG_EDGE).unwrap_or(u32::MAX);
    (1..=longest)
        .rev()
        .find(|whole| f64::from(*whole) <= value)
        .unwrap_or(1)
}

/// Encodes fresh pixels as an admissible derivative: a fitting PNG for transparency or when `lossless` asks, else a
/// JPEG at the first quality that fits.
///
/// # Errors
/// [`Reason::TooLarge`] when no encoding fits.
pub fn encode(fresh: Fresh, lossless: bool) -> Result<Derivative, Reason> {
    let opaque = match fresh {
        Fresh::Rgb(image) => {
            if lossless && let Some(found) = png(&image, ExtendedColorType::Rgb8) {
                return Ok(found);
            }
            image
        }
        Fresh::Rgba(image) => {
            if let Some(found) = png(&image, ExtendedColorType::Rgba8) {
                return Ok(found);
            }
            on_white(&image)
        }
    };
    JPEG_QUALITIES
        .into_iter()
        .find_map(|quality| jpeg(&opaque, quality))
        .ok_or(Reason::TooLarge)
}

fn png<P: Pixel<Subpixel = u8>>(
    image: &image::ImageBuffer<P, Vec<u8>>,
    color: ExtendedColorType,
) -> Option<Derivative> {
    let mut bytes = Vec::new();
    PngEncoder::new_with_quality(&mut bytes, CompressionType::Best, PngFilter::Adaptive)
        .write_image(image.as_raw(), image.width(), image.height(), color)
        .ok()?;
    fitting(MediaType::Png, image.width(), image.height(), bytes)
}

fn jpeg(image: &RgbImage, quality: u8) -> Option<Derivative> {
    let mut bytes = Vec::new();
    JpegEncoder::new_with_quality(&mut bytes, quality)
        .write_image(
            image.as_raw(),
            image.width(),
            image.height(),
            ExtendedColorType::Rgb8,
        )
        .ok()?;
    fitting(MediaType::Jpeg, image.width(), image.height(), bytes)
}

fn fitting(media_type: MediaType, width: u32, height: u32, bytes: Vec<u8>) -> Option<Derivative> {
    (bytes.len() <= MAX_DERIVATIVE_BYTES).then_some(Derivative {
        media_type,
        width,
        height,
        bytes,
    })
}

/// The pixels composited over white, as a JPEG shows them.
fn on_white(image: &RgbaImage) -> RgbImage {
    let mut opaque = RgbImage::new(image.width(), image.height());
    for (source, target) in image.pixels().zip(opaque.pixels_mut()) {
        let alpha = u32::from(source[3]);
        let channel = |value: u8| {
            let blended = (u32::from(value) * alpha + 255 * (255 - alpha) + 127) / 255;
            u8::try_from(blended).unwrap_or(u8::MAX)
        };
        *target = Rgb([channel(source[0]), channel(source[1]), channel(source[2])]);
    }
    opaque
}

/// Copies `pixels` (RGBA rows) into opaque fresh pixels, dropping alpha.
#[must_use]
pub fn rgb_from_rgba(width: u32, height: u32, pixels: Vec<u8>) -> Option<RgbImage> {
    RgbaImage::from_raw(width, height, pixels).map(|rgba| DynamicImage::ImageRgba8(rgba).to_rgb8())
}

#[cfg(test)]
mod tests;
