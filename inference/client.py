"""Narrow Team Controller client for the isolated LangGraph Brain runtime."""

from __future__ import annotations

import http.client
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

RUNTIME_URL = os.environ.get("SHIMPZ_BRAIN_RUNTIME_URL", "http://brain-runtime:8080")
TOKEN_FILE = Path(os.environ.get("SHIMPZ_BRAIN_RUNTIME_TOKEN_FILE", "/run/shimpz-brain-runtime/token"))
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REPLY_CHARS = 60_000
MAX_ACTION_REQUESTS = 64
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
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
