"""Render one page of a PDF into a bounded image; runs only inside the preparation helper (ADR-0093).

PDFium parses the document and draws the page into a bitmap whose size is fixed here before it is allocated: the page's
own proportions within the derivative's long edge and pixel count. No form, script, or XFA engine runs; the build has
none. Only the page's pixels leave, re-encoded fresh as the image derivative is.
"""

import ctypes
import math

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from PIL import Image

from prepare import image, limits

# A page smaller than the derivative is drawn larger, up to this factor: a PDF page is vector, so the larger drawing
# stays sharp, while a page a few points wide never becomes a bitmap of mostly scaling.
MAX_PAGE_SCALE = 4.0
_WHITE = 0xFFFFFFFF


class PageRefusedError(ValueError):
    """The page cannot be rendered within the closed v1 rules."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def bitmap_size(width: float, height: float) -> tuple[int, int]:
    """The pixel size one page of `width` by `height` points is drawn at, or PageRefusedError when it has none."""
    if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
        raise PageRefusedError("unreadable")
    scale = min(
        MAX_PAGE_SCALE,
        limits.DERIVATIVE_LONG_EDGE / max(width, height),
        math.sqrt(limits.DERIVATIVE_PIXELS / (width * height)),
    )
    # Both sides round down, so the bitmap never exceeds the derivative's long edge or pixel count.
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def prepare(data: bytes, page_number: int) -> dict[str, object]:
    """Return page `page_number` (from 1) as a bounded JPEG or PNG derivative, with the document's page count."""
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        # A document that needs a password to open is encrypted; any other load failure is unreadable. PDFium's last
        # error is read in the helper's fresh process for this one file, so no earlier document can stand behind it.
        if getattr(exc, "err_code", None) == pdfium_raw.FPDF_ERR_PASSWORD:
            raise PageRefusedError("encrypted") from exc
        raise
    try:
        # A document with any security handler is refused, even one whose user password is empty.
        if pdfium_raw.FPDF_GetSecurityHandlerRevision(document.raw) != -1:
            raise PageRefusedError("encrypted")
        page_count = len(document)
        if page_count > limits.MAX_PDF_PAGES:
            raise PageRefusedError("too_large")
        if page_number > page_count:
            raise PageRefusedError("unsupported")
        page = document[page_number - 1]
        try:
            drawn = _draw(page.raw, bitmap_size(*page.get_size()))
        finally:
            page.close()
    finally:
        document.close()
    # A page is mostly text and flat color, which a PNG keeps sharp, and often smaller than a JPEG would be.
    return {**image.encode(drawn, lossless=True), "page_count": page_count}


def _draw(page: object, size: tuple[int, int]) -> Image.Image:
    """Draw the page on white into a new bitmap of exactly `size` and copy its pixels out."""
    width, height = size
    bitmap = pdfium_raw.FPDFBitmap_Create(width, height, 0)
    if not bitmap:
        raise MemoryError("the page bitmap could not be allocated")
    try:
        pdfium_raw.FPDFBitmap_FillRect(bitmap, 0, 0, width, height, _WHITE)
        pdfium_raw.FPDF_RenderPageBitmap(bitmap, page, 0, 0, width, height, 0, pdfium_raw.FPDF_ANNOT)
        stride = pdfium_raw.FPDFBitmap_GetStride(bitmap)
        pixels = ctypes.string_at(pdfium_raw.FPDFBitmap_GetBuffer(bitmap), stride * height)
    finally:
        pdfium_raw.FPDFBitmap_Destroy(bitmap)
    return Image.frombuffer("RGB", size, pixels, "raw", "BGRX", stride, 1).copy()
