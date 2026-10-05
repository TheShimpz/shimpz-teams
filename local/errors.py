"""Typed public failures shared by the local controller and HTTP adapter.

A failure the controller reports from more than one place is built here once, so it always carries one status,
message, and code; Admin localizes the code.
"""

import functools
from http import HTTPStatus


class ApiProblemError(RuntimeError):
    def __init__(self, status: HTTPStatus, message: str, *, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


stored_input_unavailable = functools.partial(
    ApiProblemError,
    HTTPStatus.SERVICE_UNAVAILABLE,
    "Assistant Stored Input state is unavailable",
    code="assistant-stored-input-state-unavailable",
)
