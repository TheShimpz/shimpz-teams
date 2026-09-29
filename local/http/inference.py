"""Local Team inference-settings routes: the model selection and the standing instructions (ADR-0083)."""

from __future__ import annotations

from http import HTTPStatus

from local.validation import validate_team_id

# Sixteen rules of 280 characters stay far below this even at four UTF-8 bytes per character plus JSON quoting.
MAX_INSTRUCTIONS_BODY_BYTES = 32 * 1024


_ROUTES = {
    ("GET", ""): ("inference-status", lambda controller, team_id, _handler: controller.inference_status(team_id)),
    ("PUT", ""): (
        "inference-configure",
        lambda controller, team_id, handler: controller.configure_inference(team_id, handler._body()),
    ),
    ("GET", "instructions"): (
        "inference-instructions-status",
        lambda controller, team_id, _handler: controller.instructions_status(team_id),
    ),
    ("PUT", "instructions"): (
        "inference-instructions-configure",
        lambda controller, team_id, handler: controller.configure_instructions(team_id, handler._body()),
    ),
}


def route(handler, parts: list[str]) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
    if len(parts) not in {4, 5} or parts[:2] != ["v1", "teams"] or parts[3] != "inference":
        return None
    team_id = validate_team_id(parts[2])
    selected = _ROUTES.get((handler.command, "/".join(parts[4:])))
    if selected is None:
        return None
    operation, call = selected
    return HTTPStatus.OK, call(handler.server.controller, team_id, handler), operation, team_id, None
