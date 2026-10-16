"""Chunked NDJSON responses for Local turns: advisory progress, then one authoritative terminal record."""

import contextlib
from collections.abc import Callable
from http import HTTPStatus

from chat import progress as chat_progress
from core.http import stdlib
from local.errors import DOCKER_UNAVAILABLE_ERRORS
from local.errors import ApiProblemError as ApiProblem
from local.http import dispatch as local
from local.http.audit import RequestAudit
from protocol.http.v1 import progress as progress_contract

Execute = Callable[[chat_progress.Reporter], tuple[HTTPStatus, dict[str, object]]]


def _write_record(handler, record: dict[str, object]) -> None:
    encoded = progress_contract.encode_record(record)
    handler.wfile.write(f"{len(encoded):X}\r\n".encode("ascii"))
    handler.wfile.write(encoded)
    handler.wfile.write(b"\r\n")
    handler.wfile.flush()


def respond(handler, operation: str, team_id: str, request_audit: RequestAudit, execute: Execute) -> None:
    """Contain stream failures after the first response byte and never re-enter HTTP dispatch."""
    completed = False
    with contextlib.suppress(Exception):
        _write(handler, operation, team_id, request_audit, execute)
        completed = True
    if not completed:
        handler.close_connection = True


def _write(handler, operation: str, team_id: str, request_audit: RequestAudit, execute: Execute) -> None:
    """Push advisory progress followed by one authoritative terminal record."""
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_private_headers()
    handler.end_headers()
    writable = True

    def emit_progress(event: dict[str, object]) -> None:
        nonlocal writable
        if not writable:
            return
        try:
            _write_record(handler, {"type": "progress", **event})
        except OSError, progress_contract.ProgressContractError:
            writable = False

    terminal: dict[str, object] = {}

    def run() -> None:
        status, payload = execute(chat_progress.Reporter(emit_progress))
        trace_id = request_audit.record(operation, result="ok", team_id=team_id)
        payload["trace_id"] = trace_id
        terminal.update(type="terminal", status=int(status), body=payload)

    def emit_failure(failure: stdlib.HttpFailure) -> None:
        trace_id = request_audit.record(operation, result=failure.result, team_id=team_id, detail=failure.audit_reason)
        terminal.update(type="terminal", status=int(failure.status), body=local.failure_payload(failure, trace_id))

    stdlib.dispatch(
        run,
        classify=lambda exc: local.classify_failure(exc, ApiProblem, DOCKER_UNAVAILABLE_ERRORS),
        emit=emit_failure,
        unexpected_message="internal error",
    )
    if writable:
        try:
            _write_record(handler, terminal)
        except OSError, progress_contract.ProgressContractError:
            writable = False
    if writable:
        with contextlib.suppress(OSError):
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
