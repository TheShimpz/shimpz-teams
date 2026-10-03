"""Hostile and ordinary attachment fixtures built in memory for preparation tests (ADR-0093)."""

from __future__ import annotations

import io

from PIL import Image
from pypdf import PdfWriter


def image(image_format: str, size: tuple[int, int], *, mode: str = "RGB", **options: object) -> bytes:
    """Encode one solid image in a given format."""
    buffer = io.BytesIO()
    color = (10, 20, 30, 128) if mode == "RGBA" else (10, 20, 30)
    Image.new(mode, size, color).save(buffer, image_format, **options)
    return buffer.getvalue()


def oriented_jpeg(size: tuple[int, int], orientation: int) -> bytes:
    """A JPEG whose EXIF orientation asks a viewer to rotate it, plus a GPS-like private tag."""
    source = Image.new("RGB", size, (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "Private Camera Maker"
    buffer = io.BytesIO()
    source.save(buffer, "JPEG", exif=exif.tobytes(), icc_profile=b"private-icc-profile")
    return buffer.getvalue()


def animated_webp() -> bytes:
    frames = [Image.new("RGB", (16, 16), (index * 40, 0, 0)) for index in range(3)]
    buffer = io.BytesIO()
    frames[0].save(buffer, "WEBP", save_all=True, append_images=frames[1:], duration=50, loop=0)
    return buffer.getvalue()


def text_pdf(*pages: str) -> bytes:
    """A small hand-written PDF whose pages show the given text with a standard font."""
    objects: list[bytes] = []
    kids = []
    for index, text in enumerate(pages):
        page_id = 4 + index * 2
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objects.append(
            f"{page_id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >> endobj\n".encode()
        )
        objects.append(
            f"{content_id} 0 obj << /Length {len(stream)} >> stream\n".encode() + stream + b"\nendstream endobj\n"
        )
    head = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >> endobj\n".encode(),
        b"3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    return _with_xref([*head, *objects])


def _with_xref(objects: list[bytes]) -> bytes:
    """Lay out numbered objects with an exact cross-reference table and trailer."""
    document = bytearray(b"%PDF-1.4\n")
    offsets = []
    for item in objects:
        offsets.append(len(document))
        document.extend(item)
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    document.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    document.extend(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(document)


def blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def encrypted_pdf() -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(text_pdf("Secret quarterly figures")))
    writer.encrypt("owner-only")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
