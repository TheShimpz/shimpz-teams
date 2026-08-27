"""Hosted HTTP routes for Assistant Stored Input metadata and deletion."""

from __future__ import annotations

from http import HTTPStatus
from typing import Protocol

from hosted.assistant import runtime as hosted_assistants
from hosted.chat import api as hosted_chat_api
from hosted.team import resources as hosted_resources


class _AuthorizedRequest(Protocol):
    params: dict[str, str]
    team_id: str
    lease: hosted_resources._AuthorizationLease


class _RouteIO(Protocol):
    def _send_json(self, status: HTTPStatus, payload: dict, *, no_store: bool = False) -> None: ...

    def _audit_security(self, operation: str, team_id: str, **fields: object) -> None: ...


def list_stored_inputs(handler: _RouteIO, request: _AuthorizedRequest) -> None:
    handler._send_json(
        HTTPStatus.OK,
        hosted_assistants._assistant_stored_input_inventory(request.team_id, request.lease),
        no_store=True,
    )


def clear_stored_input(handler: _RouteIO, request: _AuthorizedRequest) -> None:
    assistant_id = request.params["assistant_id"]
    stored_input_id = request.params["stored_input_id"]
    result = hosted_chat_api._clear_assistant_stored_input(
        request.team_id,
        assistant_id,
        stored_input_id,
        request.lease,
    )
    handler._audit_security(
        "assistant_stored_input_clear",
        request.team_id,
        result="ok",
        assistant=assistant_id,
        stored_input=stored_input_id,
        cleared=result["cleared"],
    )
    handler._send_json(HTTPStatus.OK, result, no_store=True)
