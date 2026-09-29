"""Local Team inference-settings routes: the model selection and its chat reasoning effort."""

from __future__ import annotations

from http import HTTPStatus

from local.validation import validate_team_id

_ROUTES = {
    "GET": ("inference-status", lambda controller, team_id, _handler: controller.inference_status(team_id)),
    "PUT": (
        "inference-configure",
        lambda controller, team_id, handler: controller.configure_inference(team_id, handler._body()),
    ),
}


def route(handler, parts: list[str]) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
    if len(parts) != 4 or parts[:2] != ["v1", "teams"] or parts[3] != "inference":
        return None
    team_id = validate_team_id(parts[2])
    selected = _ROUTES.get(handler.command)
    if selected is None:
        return None
    operation, call = selected
    return HTTPStatus.OK, call(handler.server.controller, team_id, handler), operation, team_id, None
