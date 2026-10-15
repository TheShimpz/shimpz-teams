"""Admit one bounded line of public text, refusing anything else with the caller's own domain error.

A bound is either UTF-8 bytes or characters, as each caller's contract states; the two are never conflated.
"""


def require(
    value: object,
    error: type[Exception],
    message: str,
    *,
    max_bytes: int | None = None,
    max_chars: int | None = None,
) -> str:
    """A non-empty printable string without surrounding whitespace within its bound; ``error(message)`` otherwise."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or (max_bytes is not None and len(value.encode("utf-8")) > max_bytes)
        or (max_chars is not None and len(value) > max_chars)
    ):
        raise error(message)
    return value
