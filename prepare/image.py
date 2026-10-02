"""Decode one image and re-encode fresh pixels without metadata; runs only inside the preparation helper."""

from __future__ import annotations

import base64
import io
import math
import warnings

from PIL import Image, ImageOps

from prepare import limits

_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_JPEG_QUALITIES = (85, 75, 65, 55, 45)


class ImageRefusedError(ValueError):
    """The image cannot be read within the closed v1 rules."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def prepare(data: bytes) -> dict[str, object]:
    """Return one bounded JPEG or PNG derivative of a JPEG, PNG, or static WebP original."""
    Image.MAX_IMAGE_PIXELS = limits.MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            _admit(source)
            source.load()
            oriented = ImageOps.exif_transpose(source)
    return _encode(_fresh(oriented))


def _admit(source: Image.Image) -> None:
    if source.format not in _FORMATS:
        raise ImageRefusedError("unsupported")
    if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) != 1:
        raise ImageRefusedError("animated")
    width, height = source.size
    if max(width, height) > limits.MAX_IMAGE_EDGE or width * height > limits.MAX_IMAGE_PIXELS:
        raise ImageRefusedError("too_large")


def _fresh(oriented: Image.Image) -> Image.Image:
    """Copy decoded pixels into a new image, so no EXIF, XMP, ICC, or text chunk can follow them."""
    alpha = oriented.mode in {"RGBA", "LA", "PA"} or (oriented.mode == "P" and "transparency" in oriented.info)
    mode = "RGBA" if alpha else "RGB"
    converted = oriented.convert(mode)
    scale = min(
        1.0,
        limits.DERIVATIVE_LONG_EDGE / max(converted.size),
        math.sqrt(limits.DERIVATIVE_PIXELS / (converted.size[0] * converted.size[1])),
    )
    size = (max(1, math.floor(converted.size[0] * scale)), max(1, math.floor(converted.size[1] * scale)))
    resized = converted.resize(size, Image.Resampling.LANCZOS) if size != converted.size else converted
    return Image.frombytes(mode, resized.size, resized.tobytes())


def _encode(fresh: Image.Image) -> dict[str, object]:
    if fresh.mode == "RGBA":
        encoded = _save(fresh, "PNG", optimize=True)
        if len(encoded) <= limits.MAX_DERIVATIVE_BYTES:
            return _derivative("image/png", fresh, encoded)
        backdrop = Image.new("RGB", fresh.size, (255, 255, 255))
        backdrop.paste(fresh, mask=fresh.getchannel("A"))
        fresh = backdrop
    for quality in _JPEG_QUALITIES:
        encoded = _save(fresh, "JPEG", quality=quality, optimize=True)
        if len(encoded) <= limits.MAX_DERIVATIVE_BYTES:
            return _derivative("image/jpeg", fresh, encoded)
    raise ImageRefusedError("too_large")


def _save(image: Image.Image, image_format: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, image_format, **options)
    return buffer.getvalue()


def _derivative(media_type: str, image: Image.Image, encoded: bytes) -> dict[str, object]:
    return {
        "type": "image",
        "media_type": media_type,
        "width": image.size[0],
        "height": image.size[1],
        "base64": base64.b64encode(encoded).decode("ascii"),
    }
