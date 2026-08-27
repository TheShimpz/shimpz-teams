"""Bounded presentation intelligence that never grants Assistant authority."""

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


@dataclass(frozen=True, slots=True)
class CapabilityPlanSnapshot:
    network_id: str
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


def _capability_candidate(value: object) -> brain_runtime_client.RuntimeCapabilityCandidate:
    if not isinstance(value, dict) or set(value) != {"id", "name", "summary", "actions", "integrations"}:
        raise ValueError("invalid capability candidate")
    actions = value["actions"]
    integrations = value["integrations"]
    if not isinstance(actions, list) or not isinstance(integrations, list):
        raise ValueError("invalid capability candidate")
    projected_integrations: list[brain_runtime_client.RuntimeCapabilityIntegration] = []
    for integration in integrations:
        if not isinstance(integration, dict) or set(integration) != {"id", "provider"}:
            raise ValueError("invalid capability Integration")
        projected_integrations.append(
            brain_runtime_client.RuntimeCapabilityIntegration(
                id=integration["id"],
                provider=integration["provider"],
            )
        )
    return brain_runtime_client.RuntimeCapabilityCandidate(
        id=value["id"],
        name=value["name"],
        summary=value["summary"],
        actions=tuple(actions),
        integrations=tuple(projected_integrations),
    )


def _capability_plan_input(
    body: object,
) -> tuple[str, tuple[brain_runtime_client.RuntimeCapabilityCandidate, ...]]:
    if not isinstance(body, dict) or set(body) != {"objective", "candidates"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "capability plan requires only objective and candidates",
            code="invalid-body",
        )
    candidates = body["candidates"]
    if not isinstance(candidates, list):
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "capability plan candidates are invalid",
            code="invalid-body",
        )
    try:
        projected = tuple(_capability_candidate(value) for value in candidates)
        return brain_runtime_client.BrainRuntimeClient.validate_capability_plan_inputs(
            body["objective"],
            projected,
        )
    except (brain_runtime_client.BrainRuntimeError, TypeError, ValueError) as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "capability plan input is invalid",
            code="invalid-body",
        ) from exc


def _capability_plan_snapshot(self, team_id: str, provider: str) -> CapabilityPlanSnapshot:
    with self._lock(team_id):
        network = self.assistant_lifecycle._network(team_id)
        self.assistant_lifecycle._validate_network(network, team_id, refresh=False)
        network_id = getattr(network, "id", None)
        if not isinstance(network_id, str) or not network_id:
            raise ApiProblem(HTTPStatus.CONFLICT, "Team resource ownership conflict", code="ownership-conflict")
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
        return CapabilityPlanSnapshot(network_id, config.provider, config.model)


def capability_plan(
    self,
    team_id: str,
    body: object,
    provider: str,
    api_key: str,
) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    objective, candidates = _capability_plan_input(body)
    before = self._capability_plan_snapshot(team_id, provider)
    try:
        plan = self.brain_runtime.capability_plan(
            provider=before.provider,
            model=before.model,
            api_key=api_key,
            objective=objective,
            candidates=candidates,
        )
    except brain_runtime_client.BrainRuntimeError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant capability planning is unavailable",
            code="capability-plan-unavailable",
        ) from exc
    after = self._capability_plan_snapshot(team_id, provider)
    if after != before:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
    return {
        "team_id": team_id,
        "status": plan.status,
        "assistant_ids": list(plan.assistant_ids),
    }
