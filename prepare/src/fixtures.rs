//! Ordinary and hostile attachment fixtures built in memory, plus the committed hostile corpus (feature `testing`).
//!
//! The corpus under `tests/corpus` holds what is impractical to build here: an animated WebP, two encrypted PDFs (an
//! owner-only one and one whose user password is empty), image headers declaring 100,000 × 100,000 and 65,000 ×
//! 65,000 pixels, a PDF page whose content stream inflates to 64 MiB, and a PDF that asks for JavaScript and XFA.

use std::io::Cursor;

use image::codecs::jpeg::JpegEncoder;
use image::codecs::png::PngEncoder;
use image::codecs::webp::WebPEncoder;
use image::{ExtendedColorType, ImageEncoder, Rgb, RgbImage, Rgba, RgbaImage};

/// An animated WebP of three frames.
pub const ANIMATED_WEBP: &[u8] = include_bytes!("../tests/corpus/animated.webp");
/// A PDF encrypted with an owner and a user password.
pub const ENCRYPTED_PDF: &[u8] = include_bytes!("../tests/corpus/encrypted.pdf");
/// An encrypted PDF whose user password is empty: any reader opens it without asking for one.
pub const OPENLY_ENCRYPTED_PDF: &[u8] = include_bytes!("../tests/corpus/openly-encrypted.pdf");
/// A PNG whose header declares 100,000 × 100,000 pixels over a few bytes.
pub const PIXEL_BOMB_PNG: &[u8] = include_bytes!("../tests/corpus/pixel-bomb.png");
/// A JPEG whose frame header declares 65,000 × 65,000 pixels.
pub const PIXEL_BOMB_JPEG: &[u8] = include_bytes!("../tests/corpus/pixel-bomb.jpg");
/// A PDF whose one page's content stream inflates to 64 MiB.
pub const FLATE_BOMB_PDF: &[u8] = include_bytes!("../tests/corpus/flate-bomb.pdf");
/// A PDF that asks for JavaScript on open and carries an XFA form; its page says `Scripted page`.
pub const SCRIPTED_PDF: &[u8] = include_bytes!("../tests/corpus/scripted.pdf");
/// A GIF header: an image format outside the closed v1 rules.
pub const GIF: &[u8] = b"GIF89a\x04\x00\x04\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x04\x00\x04\x00\x00\x02\x04\x84\x8f\xa9\x0b\x00;";

/// The format of a solid fixture image.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Format {
    /// JPEG, quality 90.
    Jpeg,
    /// PNG.
    Png,
    /// Lossless static WebP.
    Webp,
}

/// One solid image, opaque (`alpha` false) or half transparent, encoded in `format`.
///
/// # Panics
/// When the encoder fails, which a fixture never makes it do.
#[must_use]
pub fn image(format: Format, width: u32, height: u32, alpha: bool) -> Vec<u8> {
    let mut out = Vec::new();
    let (pixels, color) = if alpha {
        let image = RgbaImage::from_pixel(width, height, Rgba([10, 20, 30, 128]));
        (image.into_raw(), ExtendedColorType::Rgba8)
    } else {
        let image = RgbImage::from_pixel(width, height, Rgb([10, 20, 30]));
        (image.into_raw(), ExtendedColorType::Rgb8)
    };
    let written = match format {
        Format::Jpeg => {
            JpegEncoder::new_with_quality(&mut out, 90).write_image(&pixels, width, height, color)
        }
        Format::Png => PngEncoder::new(&mut out).write_image(&pixels, width, height, color),
        Format::Webp => {
            WebPEncoder::new_lossless(&mut out).write_image(&pixels, width, height, color)
        }
    };
    written.expect("a fixture image encodes");
    out
}

/// A red JPEG whose EXIF orientation asks a viewer to rotate it, with a camera maker tag and an ICC profile.
///
/// # Panics
/// When the encoder fails, which a fixture never makes it do.
#[must_use]
pub fn oriented_jpeg(width: u32, height: u32, orientation: u16) -> Vec<u8> {
    let image = RgbImage::from_pixel(width, height, Rgb([200, 10, 10]));
    let mut encoded = Vec::new();
    JpegEncoder::new_with_quality(Cursor::new(&mut encoded), 90)
        .write_image(image.as_raw(), width, height, ExtendedColorType::Rgb8)
        .expect("a fixture image encodes");
    let maker = b"Private Camera Maker\0";
    let mut tiff = b"MM\x00\x2a\x00\x00\x00\x08\x00\x02".to_vec();
    // Make: ASCII, at the offset after the two entries and the next-IFD link.
    tiff.extend_from_slice(b"\x01\x0f\x00\x02");
    tiff.extend_from_slice(&u32::try_from(maker.len()).unwrap_or_default().to_be_bytes());
    tiff.extend_from_slice(&38_u32.to_be_bytes());
    // Orientation: one SHORT, inline.
    tiff.extend_from_slice(b"\x01\x12\x00\x03\x00\x00\x00\x01");
    tiff.extend_from_slice(&orientation.to_be_bytes());
    tiff.extend_from_slice(b"\x00\x00\x00\x00\x00\x00");
    tiff.extend_from_slice(maker);
    let mut exif = b"Exif\x00\x00".to_vec();
    exif.extend_from_slice(&tiff);
    let mut icc = b"ICC_PROFILE\x00\x01\x01".to_vec();
    icc.extend_from_slice(b"private-icc-profile");
    let mut out = encoded[..2].to_vec();
    for (marker, payload) in [(0xe1_u8, exif), (0xe2, icc)] {
        out.extend_from_slice(&[0xff, marker]);
        out.extend_from_slice(
            &u16::try_from(payload.len() + 2)
                .unwrap_or_default()
                .to_be_bytes(),
        );
        out.extend_from_slice(&payload);
    }
    out.extend_from_slice(&encoded[2..]);
    out
}

/// A small hand-written PDF whose pages show the given texts with a standard font.
#[must_use]
pub fn text_pdf(pages: &[&str]) -> Vec<u8> {
    let mut objects = vec![
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n".to_vec(),
        Vec::new(),
        b"3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n".to_vec(),
    ];
    let mut kids = Vec::new();
    for (index, text) in pages.iter().enumerate() {
        let page = 4 + index * 2;
        let content = page + 1;
        kids.push(format!("{page} 0 R"));
        let stream = format!("BT /F1 12 Tf 72 720 Td ({text}) Tj ET");
        objects.push(
            format!(
                "{page} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] \
                 /Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >> endobj\n"
            )
            .into_bytes(),
        );
        objects.push(
            format!(
                "{content} 0 obj << /Length {} >> stream\n{stream}\nendstream endobj\n",
                stream.len()
            )
            .into_bytes(),
        );
    }
    objects[1] = format!(
        "2 0 obj << /Type /Pages /Kids [{}] /Count {} >> endobj\n",
        kids.join(" "),
        pages.len()
    )
    .into_bytes();
    with_xref(&objects)
}

/// A PDF of `pages` pages, each showing `characters` letters `x` in lines of 70 that stay on the page.
#[must_use]
pub fn lined_pdf(pages: usize, characters: usize) -> Vec<u8> {
    let line = "x".repeat(70);
    let lines: Vec<String> = (0..characters / 70)
        .map(|_| format!("({line}) Tj 0 -14 Td"))
        .collect();
    let page = lines.join(" ");
    let mut objects = vec![
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n".to_vec(),
        Vec::new(),
        b"3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n".to_vec(),
    ];
    let mut kids = Vec::new();
    for index in 0..pages {
        let (id, content) = (4 + index * 2, 5 + index * 2);
        kids.push(format!("{id} 0 R"));
        let stream = format!("BT /F1 8 Tf 36 750 Td {page} ET");
        objects.push(
            format!(
                "{id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] \
                 /Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >> endobj\n"
            )
            .into_bytes(),
        );
        objects.push(
            format!(
                "{content} 0 obj << /Length {} >> stream\n{stream}\nendstream endobj\n",
                stream.len()
            )
            .into_bytes(),
        );
    }
    objects[1] = format!(
        "2 0 obj << /Type /Pages /Kids [{}] /Count {pages} >> endobj\n",
        kids.join(" ")
    )
    .into_bytes();
    with_xref(&objects)
}

/// A PDF of `pages` blank letter pages without content streams.
#[must_use]
pub fn blank_pdf(pages: usize) -> Vec<u8> {
    let kids: Vec<String> = (0..pages)
        .map(|index| format!("{} 0 R", index + 3))
        .collect();
    let mut objects = vec![
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n".to_vec(),
        format!(
            "2 0 obj << /Type /Pages /Kids [{}] /Count {pages} >> endobj\n",
            kids.join(" ")
        )
        .into_bytes(),
    ];
    for index in 0..pages {
        objects.push(
            format!(
                "{} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n",
                index + 3
            )
            .into_bytes(),
        );
    }
    with_xref(&objects)
}

/// A well-formed PDF whose page tree holds no page.
#[must_use]
pub fn pageless_pdf() -> Vec<u8> {
    with_xref(&[
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n".to_vec(),
        b"2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\n".to_vec(),
    ])
}

/// Lays out numbered objects (`1 0 obj` first) with an exact cross-reference table and trailer.
#[must_use]
pub fn with_xref(objects: &[Vec<u8>]) -> Vec<u8> {
    let mut document = b"%PDF-1.4\n".to_vec();
    let mut offsets = Vec::new();
    for object in objects {
        offsets.push(document.len());
        document.extend_from_slice(object);
    }
    let xref = document.len();
    document.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
    );
    for offset in offsets {
        document.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    document.extend_from_slice(
        format!(
            "trailer << /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        )
        .as_bytes(),
    );
    document
}

/// The pinned `PDFium` library the tests bind: the file `SHIMPZ_PDFIUM_LIBRARY` names, verified against this
/// architecture's `library_sha256` in `pdfium.toml`, so no test passes against another build.
///
/// # Panics
/// When the variable is unset, the file is unreadable, or its digest is not the pinned one.
#[must_use]
pub fn pdfium_library() -> std::path::PathBuf {
    use sha2::{Digest, Sha256};

    let path = std::env::var_os("SHIMPZ_PDFIUM_LIBRARY").map_or_else(
        || panic!("SHIMPZ_PDFIUM_LIBRARY must name the libpdfium.so that teams/prepare/pdfium.toml pins"),
        std::path::PathBuf::from,
    );
    let library = std::fs::read(&path).expect("the PDFium library is readable");
    let pins = include_str!("../pdfium.toml");
    let section = format!("[{}]", std::env::consts::ARCH);
    let pinned = pins
        .split(&section)
        .nth(1)
        .and_then(|rest| rest.lines().find(|line| line.starts_with("library_sha256")))
        .and_then(|line| line.split('"').nth(1))
        .expect("pdfium.toml pins this architecture");
    let digest = crate::content::hex(&Sha256::digest(&library));
    assert_eq!(
        digest,
        pinned,
        "{} is not the pinned PDFium build",
        path.display()
    );
    path
}
