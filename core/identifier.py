"""Admit one Team protocol identifier, refusing anything else with the caller's own domain error.

The grammars stay in the Team HTTP protocol's canonicalizers; each store or flow only names the error it raises.
"""

from collections.abc import Callable


def require(canonical: Callable[[object], str | None], value: object, error: type[Exception], message: str) -> str:
    """The canonical identifier ``canonical`` admits for ``value``; ``error(message)`` when it admits none."""
    identifier = canonical(value)
    if identifier is None:
        raise error(message)
    return identifier
