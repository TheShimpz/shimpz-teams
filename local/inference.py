"""Local Team settings: the model selection with its chat reasoning effort, and the Action confirmation policy."""

from http import HTTPStatus
from typing import NoReturn

from inference import config as inference_config
from local import audit as local_audit
from local.errors import ApiProblemError as ApiProblem
from local.errors import inference_not_configured
from local.validation import validate_team_id


def _raise_inference_problem(exc: inference_config.InferenceConfigError) -> NoReturn:
    raise ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Team model provider metadata is unavailable",
        code="inference-store-failed",
    ) from exc


def inference_status(self, team_id: str) -> dict[str, str]:
    team_id = validate_team_id(team_id)
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        try:
            config = self.inference_store.load(team_id)
        except inference_config.InferenceConfigMissingError as exc:
            raise inference_not_configured() from exc
        except inference_config.InferenceConfigError as exc:
            _raise_inference_problem(exc)
    return {"team_id": team_id, "provider": config.provider, "model": config.model, "effort": config.effort}


def configure_inference(self, team_id: str, body: object) -> dict[str, str]:
    team_id = validate_team_id(team_id)
    if not isinstance(body, dict) or set(body) != {"provider", "model", "effort"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "inference requires only provider, model, and effort",
            code="invalid-body",
        )
    if not isinstance(body["effort"], str):
        raise ApiProblem(HTTPStatus.BAD_REQUEST, "effort must be a string", code="invalid-inference")
    try:
        config = inference_config.normalize(body["provider"], body["model"], body["effort"])
    except inference_config.InferenceConfigError as exc:
        raise ApiProblem(HTTPStatus.BAD_REQUEST, str(exc), code="invalid-inference") from exc
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        try:
            self.inference_store.save(team_id, config)
        except inference_config.InferenceConfigError as exc:
            _raise_inference_problem(exc)
    return {"team_id": team_id, "provider": config.provider, "model": config.model, "effort": config.effort}


def action_confirmation_status(self, team_id: str) -> dict[str, object]:
    """Whether Team confirms this Team's mutating Actions that declare no authorization before they run."""
    team_id = validate_team_id(team_id)
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        try:
            enabled = self.inference_store.load_action_confirmation(team_id)
        except inference_config.InferenceConfigError as exc:
            _raise_inference_problem(exc)
    return {"team_id": team_id, "confirm_mutating": enabled}


def configure_action_confirmation(self, team_id: str, body: object) -> dict[str, object]:
    """Record the Supervisor's Action confirmation setting; the body is exactly ``{"confirm_mutating": bool}``."""
    team_id = validate_team_id(team_id)
    if not isinstance(body, dict) or set(body) != {"confirm_mutating"} or type(body["confirm_mutating"]) is not bool:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "the Action confirmation setting requires only a boolean confirm_mutating",
            code="invalid-body",
        )
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        try:
            enabled = self.inference_store.save_action_confirmation(team_id, body["confirm_mutating"])
        except inference_config.InferenceConfigError as exc:
            _raise_inference_problem(exc)
    local_audit.record_request(
        "action-confirmation",
        result="ok",
        team_id=team_id,
        detail="enabled" if enabled else "disabled",
    )
    return {"team_id": team_id, "confirm_mutating": enabled}
