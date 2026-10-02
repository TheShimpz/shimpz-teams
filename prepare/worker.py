"""The preparation helper's fixed entrypoint: one bounded file in, one closed derivative out (ADR-0093).

It runs as a fresh process per file inside a networkless, read-only, non-root helper container. Pillow and pypdf are
imported only here, never by the controller. Standard output carries exactly one JSON answer; anything else, a
non-zero exit, or a timeout is a refused file.
"""

from __future__ import annotations

import json
import logging
import resource
import struct
import sys
import warnings

from prepare import limits

_SIZE_LIMITS = {"image": limits.MAX_IMAGE_BYTES, "pdf": limits.MAX_PDF_BYTES}
_UNREADABLE = {"type": "opaque", "reason": "unreadable"}
# Decoder failures on hostile input; each refuses only the one file.
_DECODER_ERRORS = (
    ArithmeticError,
    EOFError,
    IndexError,
    KeyError,
    LookupError,
    MemoryError,
    OSError,
    RecursionError,
    SyntaxError,
    TypeError,
    ValueError,
    struct.error,
)


def encode_request(kind: str, data: bytes) -> bytes:
    """Frame one helper request: a bounded JSON header line, then the original bytes."""
    header = json.dumps({"kind": kind, "size": len(data)}, separators=(",", ":")).encode("ascii")
    return header + b"\n" + data


def decode_request(raw: bytes) -> tuple[str, bytes]:
    """Return the kind and exact bytes of one framed request, or raise ValueError."""
    header, separator, data = raw.partition(b"\n")
    if not separator or len(header) > limits.MAX_HELPER_HEADER_BYTES:
        raise ValueError("helper request is invalid")
    fields = json.loads(header)
    if not isinstance(fields, dict) or set(fields) != {"kind", "size"} or fields["kind"] not in _SIZE_LIMITS:
        raise ValueError("helper request is invalid")
    size = fields["size"]
    if type(size) is not int or not 1 <= size <= _SIZE_LIMITS[fields["kind"]] or size != len(data):
        raise ValueError("helper request is invalid")
    return fields["kind"], data


def answer(raw: bytes) -> dict[str, object]:
    """Prepare one framed file into its closed answer."""
    try:
        kind, data = decode_request(raw)
    except ValueError:
        return dict(_UNREADABLE)
    return _image(data) if kind == "image" else _pdf(data)


def _image(data: bytes) -> dict[str, object]:
    from PIL import Image

    from prepare import image

    try:
        return image.prepare(data)
    except image.ImageRefusedError as exc:
        return {"type": "opaque", "reason": exc.reason}
    except Image.DecompressionBombError, Image.DecompressionBombWarning:
        return {"type": "opaque", "reason": "too_large"}
    except _DECODER_ERRORS:
        return dict(_UNREADABLE)


def _pdf(data: bytes) -> dict[str, object]:
    from pypdf import errors

    from prepare import pdf

    try:
        return pdf.prepare(data)
    except pdf.PdfRefusedError as exc:
        return {"type": "opaque", "reason": exc.reason}
    except errors.FileNotDecryptedError:
        return {"type": "opaque", "reason": "encrypted"}
    except errors.LimitReachedError:
        return {"type": "opaque", "reason": "too_large"}
    except (errors.PyPdfError, *_DECODER_ERRORS):
        return dict(_UNREADABLE)


def _confine() -> None:
    """Bound this process to its per-file CPU budget, few descriptors, no core file, and no written file."""
    resource.setrlimit(resource.RLIMIT_CPU, (limits.HELPER_CPU_SECONDS, limits.HELPER_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # Library diagnostics would reach standard error, which the controller treats as a failed file.
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")


def main() -> int:
    """Read one request from standard input and write one answer to standard output."""
    _confine()
    raw = sys.stdin.buffer.read(limits.MAX_HELPER_INPUT_BYTES + 1)
    result = answer(raw) if len(raw) <= limits.MAX_HELPER_INPUT_BYTES else dict(_UNREADABLE)
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
