"""Extract the text of one PDF with bounded decoding; runs only inside the preparation helper."""

import io

import pypdf

from prepare import limits


class PdfRefusedError(ValueError):
    """The PDF cannot be read as text within the closed v1 rules."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_STREAM_LIMITS = {
    "maximum_declared_stream_length": limits.MAX_PDF_PAGE_STREAM_BYTES,
    "array_based_stream_maximum_output_length": limits.MAX_PDF_PAGE_STREAM_BYTES,
    "jbig2_maximum_output_length": limits.MAX_PDF_PAGE_STREAM_BYTES,
    "lzw_maximum_output_length": limits.MAX_PDF_PAGE_STREAM_BYTES,
    "run_length_maximum_output_length": limits.MAX_PDF_PAGE_STREAM_BYTES,
    "zlib_maximum_output_length": limits.MAX_PDF_PAGE_STREAM_BYTES,
    "image_maximum_buffer_size": limits.MAX_PDF_PAGE_STREAM_BYTES,
    "jbig2dec_binary": None,
    "disable_legacy_handling": True,
}


def prepare(data: bytes) -> dict[str, object]:
    """Return the text of every page, refusing encrypted, image-only, oversized, or over-long documents."""
    with pypdf.apply_configuration(**_STREAM_LIMITS):
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise PdfRefusedError("encrypted")
        if len(reader.pages) > limits.MAX_PDF_PAGES:
            raise PdfRefusedError("too_large")
        pages = _pages_text(reader.pages)
    text = "\n\n".join(page for page in pages if page)
    if not text.strip():
        raise PdfRefusedError("no_text")
    if len(text) > limits.MAX_TEXT_CHARACTERS or len(text.encode("utf-8")) > limits.MAX_TEXT_BYTES:
        raise PdfRefusedError("too_large")
    return {"type": "text", "text": text}


def _pages_text(pages: list[pypdf.PageObject]) -> list[str]:
    """Extract each page's text after bounding its decoded content stream and the document's running total."""
    texts: list[str] = []
    decoded = 0
    for page in pages:
        contents = page.get_contents()
        size = len(contents.get_data()) if contents is not None else 0
        decoded += size
        if size > limits.MAX_PDF_PAGE_STREAM_BYTES or decoded > limits.MAX_PDF_FILE_STREAM_BYTES:
            raise PdfRefusedError("too_large")
        texts.append(page.extract_text().strip())
    return texts
