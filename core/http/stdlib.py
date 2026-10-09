"""Small fail-closed primitives for stdlib HTTP control-plane services."""

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus


@dataclass(frozen=True)
class HttpFailure:
    status: HTTPStatus
    public_message: str
    audit_reason: str
    result: str
    public_code: str | None = None


def dispatch(
    action: Callable[[], None],
    *,
    classify: Callable[[Exception], HttpFailure | None],
    emit: Callable[[HttpFailure], None],
    unexpected_message: str,
) -> None:
    """Run one HTTP action and redact every unclassified ordinary exception."""
    try:
        action()
    except Exception as exc:
        failure = classify(exc)
        if failure is None:
            failure = HttpFailure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                unexpected_message,
                type(exc).__name__,
                "error",
            )
        emit(failure)
