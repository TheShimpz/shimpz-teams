"""Narrow Team Controller client for the isolated LangGraph Brain runtime."""

from __future__ import annotations

import http.client
import json
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from core import strict_json

RUNTIME_URL = os.environ.get("SHIMPZ_BRAIN_RUNTIME_URL", "http://brain-runtime:8080")
TOKEN_FILE = Path(os.environ.get("SHIMPZ_BRAIN_RUNTIME_TOKEN_FILE", "/run/shimpz-brain-runtime/token"))
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REPLY_CHARS = 60_000
MAX_ACTION_REQUESTS = 64
MAX_ACTION_LABELS = 64
MAX_ACTION_LABEL_CHARS = 80
MAX_LANGUAGE_EXEMPLAR_CHARS = 2_000
MAX_CAPABILITY_CANDIDATES = 8
MAX_CAPABILITY_SELECTED = 4
MAX_CAPABILITY_OBJECTIVE_CHARS = 16_000
MAX_CAPABILITY_NAME_CHARS = 80
MAX_CAPABILITY_SUMMARY_CHARS = 160
MAX_CAPABILITY_ACTIONS = 64
MAX_CAPABILITY_INTEGRATIONS = 16
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
ACTION_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
REPLY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class BrainRuntimeError(RuntimeError):
    """The private runtime was unavailable or violated its closed response contract."""


@dataclass(frozen=True, slots=True)
class RuntimeAction:
    id: str
    summary: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeAssistant:
    id: str
    genesis: str
    actions: tuple[RuntimeAction, ...]


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    thread_id: str
    team_name: str
    assistants: tuple[RuntimeAssistant, ...]
    provider: Literal["anthropic", "openai"]
    model: str
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ActionRequest:
    interrupt_id: str
    assistant_id: str
    action: str
    input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeTurn:
    status: Literal["completed", "action-required"]
    reply: str
    actions: tuple[ActionRequest, ...]


@dataclass(frozen=True, slots=True)
class RuntimeActionLabel:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityIntegration:
    id: str
    provider: str


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityCandidate:
    id: str
    name: str
    summary: str
    actions: tuple[str, ...]
    integrations: tuple[RuntimeCapabilityIntegration, ...]


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityPlan:
    status: Literal["sufficient", "install-required"]
    assistant_ids: tuple[str, ...]


ConnectionFactory = Callable[[str, int, float], http.client.HTTPConnection]


def _connection(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port, timeout=timeout)


class BrainRuntimeClient:
    def __init__(
        self,
        *,
        base_url: str = RUNTIME_URL,
        token_file: Path = TOKEN_FILE,
        connection_factory: ConnectionFactory = _connection,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise BrainRuntimeError("Brain runtime URL is invalid")
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._token_file = token_file
        self._connection_factory = connection_factory

    def _token(self) -> str:
        try:
            token = self._token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BrainRuntimeError("Brain runtime authentication is unavailable") from exc
        if not token or len(token) > 4 * 1024 or "\0" in token:
            raise BrainRuntimeError("Brain runtime authentication is unavailable")
        return token

    @staticmethod
    def _context(context: RuntimeContext) -> dict[str, object]:
        return {
            "thread_id": context.thread_id,
            "team_name": context.team_name,
            "assistants": [
                {
                    "id": assistant.id,
                    "genesis": assistant.genesis,
                    "actions": [
                        {
                            "id": action.id,
                            "summary": action.summary,
                            "input_schema": dict(action.input_schema),
                        }
                        for action in assistant.actions
                    ],
                }
                for assistant in context.assistants
            ],
            "provider": {
                "provider": context.provider,
                "model": context.model,
                "api_key": context.api_key,
            },
        }

    def _post(self, path: str, payload: Mapping[str, object]) -> object:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        connection = self._connection_factory(self._host, self._port, 65.0)
        try:
            connection.request(
                "POST",
                path,
                body,
                {
                    "Authorization": f"Bearer {self._token()}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise BrainRuntimeError("Brain runtime is unavailable") from exc
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        if response.status != 200:
            raise BrainRuntimeError("Brain runtime request failed")
        try:
            decoded = strict_json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise BrainRuntimeError("Brain runtime returned an invalid response") from exc
        return decoded

    @staticmethod
    def _parse_turn(value: object) -> RuntimeTurn:
        if not isinstance(value, dict) or set(value) != {"status", "reply", "actions"}:
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        status = value["status"]
        reply = value["reply"]
        raw_actions = value["actions"]
        if (
            status not in {"completed", "action-required"}
            or not isinstance(reply, str)
            or not isinstance(raw_actions, list)
        ):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        if (
            len(reply) > MAX_REPLY_CHARS
            or REPLY_CONTROL_RE.search(reply) is not None
            or len(raw_actions) > MAX_ACTION_REQUESTS
        ):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        actions: list[ActionRequest] = []
        for raw in raw_actions:
            if not isinstance(raw, dict) or set(raw) != {
                "interrupt_id",
                "assistant_id",
                "action",
                "input",
            }:
                raise BrainRuntimeError("Brain runtime returned an invalid response")
            interrupt_id = raw["interrupt_id"]
            assistant_id = raw["assistant_id"]
            action = raw["action"]
            action_input = raw["input"]
            if (
                not isinstance(interrupt_id, str)
                or SAFE_ID_RE.fullmatch(interrupt_id) is None
                or not isinstance(assistant_id, str)
                or ACTION_ID_RE.fullmatch(assistant_id) is None
                or not isinstance(action, str)
                or ACTION_ID_RE.fullmatch(action) is None
                or not isinstance(action_input, dict)
            ):
                raise BrainRuntimeError("Brain runtime returned an invalid response")
            actions.append(
                ActionRequest(
                    interrupt_id=interrupt_id,
                    assistant_id=assistant_id,
                    action=action,
                    input=action_input,
                )
            )
        if status == "completed" and (not reply.strip() or actions):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        if status == "action-required" and (reply or not actions):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        return RuntimeTurn(status=status, reply=reply, actions=tuple(actions))

    @staticmethod
    def _parse_action_labels(value: object, action_ids: tuple[str, ...]) -> tuple[RuntimeActionLabel, ...]:
        if not isinstance(value, dict) or set(value) != {"labels"} or not isinstance(value["labels"], list):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        expected = frozenset(action_ids)
        labels: dict[str, str] = {}
        for item in value["labels"]:
            if not isinstance(item, dict) or set(item) != {"id", "label"}:
                raise BrainRuntimeError("Brain runtime returned an invalid response")
            action_id = item["id"]
            label = item["label"]
            if (
                not isinstance(action_id, str)
                or action_id not in expected
                or action_id in labels
                or not isinstance(label, str)
            ):
                raise BrainRuntimeError("Brain runtime returned an invalid response")
            normalized = unicodedata.normalize("NFC", label)
            if (
                normalized != label
                or normalized.strip() != normalized
                or not 1 <= len(normalized) <= MAX_ACTION_LABEL_CHARS
                or any(unicodedata.category(character).startswith("C") for character in normalized)
            ):
                raise BrainRuntimeError("Brain runtime returned an invalid response")
            labels[action_id] = normalized
        if len(labels) != len(expected) or len(set(labels.values())) != len(labels):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        return tuple(RuntimeActionLabel(action_id, labels[action_id]) for action_id in action_ids)

    @staticmethod
    def _capability_text(value: object, maximum: int, *, allow_layout: bool = False) -> str:
        if not isinstance(value, str):
            raise BrainRuntimeError("Brain runtime capability plan request is invalid")
        normalized = unicodedata.normalize("NFC", value)
        if (
            normalized != value
            or value.strip() != value
            or not 1 <= len(value) <= maximum
            or any(
                unicodedata.category(character).startswith("C")
                and (not allow_layout or character not in {"\n", "\t"})
                for character in value
            )
        ):
            raise BrainRuntimeError("Brain runtime capability plan request is invalid")
        return value

    @classmethod
    def validate_capability_plan_inputs(
        cls,
        objective: object,
        candidates: tuple[RuntimeCapabilityCandidate, ...],
    ) -> tuple[str, tuple[RuntimeCapabilityCandidate, ...]]:
        task = cls._capability_text(objective, MAX_CAPABILITY_OBJECTIVE_CHARS, allow_layout=True)
        if not isinstance(candidates, tuple) or not 1 <= len(candidates) <= MAX_CAPABILITY_CANDIDATES:
            raise BrainRuntimeError("Brain runtime capability plan request is invalid")
        admitted: list[RuntimeCapabilityCandidate] = []
        for candidate in candidates:
            if not isinstance(candidate, RuntimeCapabilityCandidate):
                raise BrainRuntimeError("Brain runtime capability plan request is invalid")
            actions = candidate.actions
            integrations = candidate.integrations
            if (
                not isinstance(candidate.id, str)
                or ACTION_ID_RE.fullmatch(candidate.id) is None
                or not isinstance(actions, tuple)
                or any(not isinstance(item, str) or ACTION_ID_RE.fullmatch(item) is None for item in actions)
                or not 1 <= len(actions) <= MAX_CAPABILITY_ACTIONS
                or actions != tuple(sorted(set(actions)))
                or not isinstance(integrations, tuple)
                or len(integrations) > MAX_CAPABILITY_INTEGRATIONS
                or any(
                    not isinstance(item, RuntimeCapabilityIntegration)
                    or not isinstance(item.id, str)
                    or ACTION_ID_RE.fullmatch(item.id) is None
                    or not isinstance(item.provider, str)
                    or ACTION_ID_RE.fullmatch(item.provider) is None
                    for item in integrations
                )
            ):
                raise BrainRuntimeError("Brain runtime capability plan request is invalid")
            if integrations != tuple(sorted(set(integrations), key=lambda item: (item.id, item.provider))):
                raise BrainRuntimeError("Brain runtime capability plan request is invalid")
            admitted.append(
                RuntimeCapabilityCandidate(
                    id=candidate.id,
                    name=cls._capability_text(candidate.name, MAX_CAPABILITY_NAME_CHARS),
                    summary=cls._capability_text(candidate.summary, MAX_CAPABILITY_SUMMARY_CHARS),
                    actions=actions,
                    integrations=integrations,
                )
            )
        result = tuple(admitted)
        if tuple(item.id for item in result) != tuple(sorted({item.id for item in result})):
            raise BrainRuntimeError("Brain runtime capability plan request is invalid")
        return task, result

    @staticmethod
    def _parse_capability_plan(
        value: object,
        candidates: tuple[RuntimeCapabilityCandidate, ...],
    ) -> RuntimeCapabilityPlan:
        if not isinstance(value, dict) or set(value) != {"status", "assistant_ids"}:
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        status = value["status"]
        raw_ids = value["assistant_ids"]
        if status not in {"sufficient", "install-required"} or not isinstance(raw_ids, list):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        expected = frozenset(candidate.id for candidate in candidates)
        assistant_ids = tuple(raw_ids)
        if (
            any(not isinstance(item, str) or item not in expected for item in assistant_ids)
            or assistant_ids != tuple(sorted(set(assistant_ids)))
            or len(assistant_ids) > MAX_CAPABILITY_SELECTED
            or (status == "sufficient") != (not assistant_ids)
        ):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        return RuntimeCapabilityPlan(status, assistant_ids)

    def start(self, context: RuntimeContext, message: str) -> RuntimeTurn:
        payload = self._context(context)
        payload["message"] = message
        return self._parse_turn(self._post("/v1/turns", payload))

    def resume(self, context: RuntimeContext, results: Mapping[str, object]) -> RuntimeTurn:
        payload = self._context(context)
        payload["results"] = dict(results)
        return self._parse_turn(self._post("/v1/turns/resume", payload))

    def delete_thread(self, thread_id: str) -> None:
        if not isinstance(thread_id, str) or SAFE_ID_RE.fullmatch(thread_id) is None:
            raise BrainRuntimeError("Brain runtime thread ID is invalid")
        response = self._post("/v1/threads/delete", {"thread_id": thread_id})
        if not isinstance(response, dict) or response != {"status": "deleted"}:
            raise BrainRuntimeError("Brain runtime returned an invalid response")

    def action_labels(
        self,
        *,
        provider: Literal["anthropic", "openai"],
        model: str,
        api_key: str,
        language_exemplar: str,
        action_ids: tuple[str, ...],
    ) -> tuple[RuntimeActionLabel, ...]:
        if (
            provider not in {"anthropic", "openai"}
            or not isinstance(model, str)
            or SAFE_ID_RE.fullmatch(model) is None
            or not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 16 * 1024
            or "\0" in api_key
            or not isinstance(language_exemplar, str)
            or language_exemplar.strip() != language_exemplar
            or not 1 <= len(language_exemplar) <= MAX_LANGUAGE_EXEMPLAR_CHARS
            or any(
                unicodedata.category(character).startswith("C") and character not in {"\n", "\t"}
                for character in language_exemplar
            )
            or not 1 <= len(action_ids) <= MAX_ACTION_LABELS
            or any(ACTION_ID_RE.fullmatch(action_id) is None for action_id in action_ids)
            or len(set(action_ids)) != len(action_ids)
        ):
            raise BrainRuntimeError("Brain runtime Action label request is invalid")
        response = self._post(
            "/v1/action-labels",
            {
                "provider": {"provider": provider, "model": model, "api_key": api_key},
                "language_exemplar": language_exemplar,
                "actions": list(action_ids),
            },
        )
        return self._parse_action_labels(response, action_ids)

    def capability_plan(
        self,
        *,
        provider: Literal["anthropic", "openai"],
        model: str,
        api_key: str,
        objective: object,
        candidates: tuple[RuntimeCapabilityCandidate, ...],
    ) -> RuntimeCapabilityPlan:
        if (
            provider not in {"anthropic", "openai"}
            or not isinstance(model, str)
            or SAFE_ID_RE.fullmatch(model) is None
            or not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 16 * 1024
            or "\0" in api_key
        ):
            raise BrainRuntimeError("Brain runtime capability plan request is invalid")
        task, admitted = self.validate_capability_plan_inputs(objective, candidates)
        response = self._post(
            "/v1/capability-plan",
            {
                "provider": {"provider": provider, "model": model, "api_key": api_key},
                "objective": task,
                "candidates": [
                    {
                        "id": candidate.id,
                        "name": candidate.name,
                        "summary": candidate.summary,
                        "actions": list(candidate.actions),
                        "integrations": [
                            {"id": integration.id, "provider": integration.provider}
                            for integration in candidate.integrations
                        ],
                    }
                    for candidate in admitted
                ],
            },
        )
        return self._parse_capability_plan(response, admitted)
