"""Local Team inference settings: the model selection and its chat reasoning effort."""

from __future__ import annotations

from http import HTTPStatus
from typing import NoReturn

from inference import config as inference_config
from local.errors import ApiProblemError as ApiProblem
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
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team model provider is not configured",
                code="inference-not-configured",
            ) from exc
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
