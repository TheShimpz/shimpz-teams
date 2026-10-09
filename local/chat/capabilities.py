"""Bounded presentation intelligence that never grants Assistant authority."""

from dataclasses import dataclass
from http import HTTPStatus

from inference import client as brain_runtime_client
from local.chat import state as local_chat_state
from local.errors import ApiProblemError as ApiProblem
from local.errors import team_context_changed
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
        _team_name, network_id, active_by_id = local_chat_state._team_assistants(self, team_id)
        active = active_by_id.get(assistant_id)
        if active is None:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "installed Assistant is unavailable",
                code="assistant-unavailable",
            )
        config = local_chat_state._turn_inference(self, team_id, provider)
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
    if not isinstance(body, dict) or set(body) != {"locale"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action labels require only locale",
            code="invalid-body",
        )
    locale = http_payload.canonical_locale(body["locale"])
    if locale is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "locale is invalid",
            code="invalid-locale",
        )
    before = self._action_label_snapshot(team_id, assistant_id, provider)
    try:
        labels = self.brain_runtime.action_labels(
            provider=before.provider,
            model=before.model,
            api_key=api_key,
            locale=locale,
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
        raise team_context_changed()
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
        _team_name, network_id, _active = local_chat_state._team_assistants(self, team_id, scan=False)
        config = local_chat_state._turn_inference(self, team_id, provider)
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
        raise team_context_changed()
    return {
        "team_id": team_id,
        "status": plan.status,
        "assistant_ids": list(plan.assistant_ids),
    }


def _directory_candidate(value: object) -> brain_runtime_client.RuntimeDirectoryCandidate:
    if not isinstance(value, dict) or set(value) != {"id", "name", "summary"}:
        raise ValueError("invalid Assistant directory candidate")
    return brain_runtime_client.RuntimeDirectoryCandidate(
        id=value["id"],
        name=value["name"],
        summary=value["summary"],
    )


def _lifecycle_reference(value: object) -> brain_runtime_client.RuntimeLifecycleReference | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"id", "name"}:
        raise ValueError("invalid Assistant lifecycle reference")
    return brain_runtime_client.RuntimeLifecycleReference(id=value["id"], name=value["name"])


def _conversation_entry(value: object) -> brain_runtime_client.RuntimeConversationEntry:
    if not isinstance(value, dict) or set(value) != {"role", "text", "truncated"}:
        raise ValueError("invalid conversation entry")
    return brain_runtime_client.RuntimeConversationEntry(
        role=value["role"],
        text=value["text"],
        truncated=value["truncated"],
    )


def _lifecycle_context(body: dict[str, object]) -> brain_runtime_client.RuntimeLifecycleContext:
    reference = _lifecycle_reference(body["lifecycle_reference"])
    conversation = body["conversation"]
    if not isinstance(conversation, list):
        raise ValueError("invalid conversation window")
    projected = tuple(_conversation_entry(entry) for entry in conversation)
    return brain_runtime_client.RuntimeLifecycleContext(reference, projected, body["locale"])


def _intent_route_input(
    body: object,
) -> tuple[
    str,
    brain_runtime_client.LifecycleIntent | None,
    tuple[brain_runtime_client.RuntimeDirectoryCandidate, ...],
    brain_runtime_client.RuntimeLifecycleContext,
]:
    if not isinstance(body, dict) or set(body) != {
        "objective",
        "expected_intent",
        "candidates",
        "lifecycle_reference",
        "conversation",
        "locale",
    }:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "intent route requires objective, expected_intent, candidates, and lifecycle context",
            code="invalid-body",
        )
    candidates = body["candidates"]
    if not isinstance(candidates, list):
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "intent route candidates are invalid",
            code="invalid-body",
        )
    try:
        projected = tuple(_directory_candidate(value) for value in candidates)
        return brain_runtime_client.BrainRuntimeClient.validate_intent_route_inputs(
            body["objective"],
            body["expected_intent"],
            projected,
            _lifecycle_context(body),
        )
    except (brain_runtime_client.BrainRuntimeError, TypeError, ValueError) as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "intent route input is invalid",
            code="invalid-body",
        ) from exc


def intent_route(
    self,
    team_id: str,
    body: object,
    provider: str,
    api_key: str,
    decision_key: str | None = None,
) -> dict[str, object]:
    """Route one objective without exposing Team state or granting lifecycle authority."""
    team_id = validate_team_id(team_id)
    objective, expected_intent, candidates, context = _intent_route_input(body)
    if decision_key is not None and expected_intent is not None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "a decision credential applies only to intent classification",
            code="invalid-decision-credential",
        )
    before = self._capability_plan_snapshot(team_id, provider)
    try:
        route = self.brain_runtime.intent_route(
            credentials=brain_runtime_client.RouteCredentials(before.provider, before.model, api_key, decision_key),
            objective=objective,
            expected_intent=expected_intent,
            candidates=candidates,
            context=context,
        )
    except brain_runtime_client.BrainRuntimeError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant lifecycle routing is unavailable",
            code="intent-route-unavailable",
        ) from exc
    after = self._capability_plan_snapshot(team_id, provider)
    if after != before:
        raise team_context_changed()
    return {
        "team_id": team_id,
        "intent": route.intent,
        "query": route.query,
        "assistant_ids": list(route.assistant_ids),
        "reply": route.reply,
        "task_follows": route.task_follows,
    }
