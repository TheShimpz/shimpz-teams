"""Presentation-only labels for the exact installed Assistant binding."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from inference import client as brain_runtime_client
from inference import config as inference_config
from local.errors import ApiProblemError as ApiProblem
from local.validation import validate_assistant_id, validate_team_id
from protocol.http.v1 import payload as http_payload


@dataclass(frozen=True, slots=True)
class ActionLabelSnapshot:
    network_id: str
    assistant_version: str
    action_ids: tuple[str, ...]
    provider: str
    model: str


def _action_label_snapshot(
    self,
    team_id: str,
    assistant_id: str,
    provider: str,
) -> ActionLabelSnapshot:
    with self._lock(team_id):
        network = self.assistant_lifecycle._network(team_id)
        self.assistant_lifecycle._validate_network(network, team_id, refresh=False)
        network_id = getattr(network, "id", None)
        if not isinstance(network_id, str) or not network_id:
            raise ApiProblem(HTTPStatus.CONFLICT, "Team resource ownership conflict", code="ownership-conflict")
        active = next(
            (
                item
                for item in self._active_chat_assistants(team_id, network.name)
                if item.spec.assistant_id == assistant_id
            ),
            None,
        )
        if active is None:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "installed Assistant is unavailable",
                code="assistant-unavailable",
            )
        try:
            config = self.inference_store.load(team_id)
        except inference_config.InferenceConfigError as exc:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team model provider is not configured",
                code="inference-not-configured",
            ) from exc
        if config.provider != provider:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "configured model provider changed; retry",
                code="inference-provider-mismatch",
            )
        return ActionLabelSnapshot(
            network_id=network_id,
            assistant_version=active.spec.version,
            action_ids=tuple(sorted(active.spec.actions)),
            provider=config.provider,
            model=config.model,
        )


def action_labels(
    self,
    team_id: str,
    assistant_id: str,
    body: object,
    provider: str,
    api_key: str,
) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    assistant_id = validate_assistant_id(assistant_id)
    if not isinstance(body, dict) or set(body) != {"language_exemplar"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action labels require only language_exemplar",
            code="invalid-body",
        )
    language_exemplar = http_payload.canonical_language_exemplar(body["language_exemplar"])
    if language_exemplar is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "language_exemplar is invalid",
            code="invalid-language-exemplar",
        )
    before = self._action_label_snapshot(team_id, assistant_id, provider)
    try:
        labels = self.brain_runtime.action_labels(
            provider=before.provider,
            model=before.model,
            api_key=api_key,
            language_exemplar=language_exemplar,
            action_ids=before.action_ids,
        )
    except brain_runtime_client.BrainRuntimeError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "installed Assistant Action labels are unavailable",
            code="action-labels-unavailable",
        ) from exc
    after = self._action_label_snapshot(team_id, assistant_id, provider)
    if after != before:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
    return {
        "team_id": team_id,
        "assistant": assistant_id,
        "assistant_version": before.assistant_version,
        "actions": [{"id": label.id, "label": label.label} for label in labels],
    }
