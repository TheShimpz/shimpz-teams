"""Local Team settings routes: the model selection with its reasoning effort, and the Action confirmation policy."""

from http import HTTPStatus

from local.validation import validate_team_id

_CONFIRMATION_ROUTES = {
    "GET": (
        "action-confirmation-status",
        lambda controller, team_id, _handler: controller.action_confirmation_status(team_id),
    ),
    "PUT": (
        "action-confirmation-configure",
        lambda controller, team_id, handler: controller.configure_action_confirmation(team_id, handler._body()),
    ),
}
_ROUTES = {
    "GET": ("inference-status", lambda controller, team_id, _handler: controller.inference_status(team_id)),
    "PUT": (
        "inference-configure",
        lambda controller, team_id, handler: controller.configure_inference(team_id, handler._body()),
    ),
}


def route(handler, parts: list[str]) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
    if len(parts) != 4 or parts[:2] != ["v1", "teams"] or parts[3] not in {"inference", "action-confirmation"}:
        return None
    team_id = validate_team_id(parts[2])
    selected = (_ROUTES if parts[3] == "inference" else _CONFIRMATION_ROUTES).get(handler.command)
    if selected is None:
        return None
    operation, call = selected
    return HTTPStatus.OK, call(handler.server.controller, team_id, handler), operation, team_id, None
