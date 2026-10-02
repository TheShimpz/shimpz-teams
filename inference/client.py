"""Narrow Team Controller client for the isolated LangGraph Brain runtime."""

from __future__ import annotations

import contextvars
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from core import strict_json
from inference import usage as brain_usage
from protocol.http.v1 import payload as http_payload

RUNTIME_URL = os.environ.get("SHIMPZ_BRAIN_RUNTIME_URL", "http://brain-runtime:8080")
TOKEN_FILE = Path(os.environ.get("SHIMPZ_BRAIN_RUNTIME_TOKEN_FILE", "/run/shimpz-brain-runtime/token"))
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REPLY_CHARS = 60_000
MAX_ACTION_REQUESTS = 64
MAX_ACTION_LABELS = 64
MAX_ACTION_LABEL_CHARS = 80
MAX_CAPABILITY_CANDIDATES = 8
MAX_CAPABILITY_SELECTED = 4
MAX_CAPABILITY_OBJECTIVE_CHARS = 16_000
MAX_CAPABILITY_NAME_CHARS = 80
MAX_CAPABILITY_SUMMARY_CHARS = 160
MAX_CAPABILITY_ACTIONS = 64
MAX_CAPABILITY_INTEGRATIONS = 16
MAX_INTENT_ROUTE_CANDIDATES = 8
MAX_INTENT_ROUTE_SELECTED = 4
MAX_INTENT_ROUTE_QUERY_CHARS = 160
MAX_INTENT_ROUTE_NAME_CHARS = 80
MAX_INTENT_ROUTE_SUMMARY_CHARS = 160
MAX_INTENT_ROUTE_REPLY_CHARS = 240
MAX_CONVERSATION_ENTRIES = 8
MAX_CONVERSATION_TEXT_CHARS = 512
MAX_CONVERSATION_CHARS = 4_096
_LANGUAGE_LAYOUT_CONTROLS = frozenset({"\n", "\r", "\t"})
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


def contract_digest(assistant: RuntimeAssistant) -> str:
    """The `sha256:` fingerprint of one Assistant contract as Brain receives it; a changed contract changes it."""
    contract = {
        "id": assistant.id,
        "genesis": assistant.genesis,
        "actions": [
            {"id": action.id, "summary": action.summary, "input_schema": dict(action.input_schema)}
            for action in assistant.actions
        ],
    }
    body = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    thread_id: str
    team_name: str
    assistants: tuple[RuntimeAssistant, ...]
    provider: Literal["anthropic", "openai"]
    model: str
    api_key: str = field(repr=False)
    effort: Literal["low", "medium", "high"]
    # The Team's learned memory (ADR-0084), already validated by its store; None withholds the Brain's memory tool.
    memories: tuple[dict[str, str], ...] | None = None
    # The learned skills usable in this turn (ADR-0085); None where learning is unavailable.
    skills: tuple[dict[str, object], ...] | None = None
    # The Team's Routines as data (ADR-0086); None withholds the Brain's Routine tool.
    routines: tuple[dict[str, object], ...] | None = None
    # False in a Routine run, whose memory and skills the Brain may read but never change.
    knowledge_writable: bool = True
    # The interface language a new turn is written in (ADR-0090), or None to follow the message; the Brain pins it at
    # the start, so only a start sends it.
    locale: str | None = None


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
    # One closed multiple-choice question that ended a completed turn (ADR-0081), or None.
    clarification: dict[str, object] | None = None
    # The memory changes a completed turn proposed; the profile saves them only when its reply commits (ADR-0084).
    memory: tuple[dict[str, str], ...] = ()
    # The one Routine change a completed turn's isolated compiler produced (ADR-0092), or None; Local Team admits it.
    routine: dict[str, object] | None = None


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


LifecycleIntent = Literal["assistant-install", "assistant-uninstall"]
RouteIntent = Literal["ordinary-task", "assistant-install", "assistant-uninstall", "unresolved"]


@dataclass(frozen=True, slots=True)
class RuntimeDirectoryCandidate:
    id: str
    name: str
    summary: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleReference:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class RuntimeConversationEntry:
    role: Literal["user", "assistant"]
    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleContext:
    reference: RuntimeLifecycleReference | None = None
    conversation: tuple[RuntimeConversationEntry, ...] = ()
    # The interface language every route reply is written in (ADR-0090); required by every route request.
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeIntentRoute:
    intent: RouteIntent
    query: str = ""
    assistant_ids: tuple[str, ...] = ()
    reply: str = ""
    task_follows: bool = False


ConnectionFactory = Callable[[str, int, float], http.client.HTTPConnection]

# Brain shares a local network with Team, so a connection is established quickly or not at all; a turn then may
# legitimately wait on the model provider.
CONNECT_TIMEOUT_SECONDS = 5.0
RESPONSE_TIMEOUT_SECONDS = 65.0
# An optional purpose sentence may delay a person's prompt only this long in total, connection included (ADR-0090).
PURPOSE_DEADLINE_SECONDS = 15.0


class RequestAbort:
    """Stop's handle on the Brain request a Local chat turn is waiting for (ADR-0079).

    ``abort`` shuts down the attached connection's socket, which wakes the blocked read and makes Brain see the
    disconnect and cancel the turn's provider call. A request attached after the abort fails before connecting, and
    one still connecting fails as soon as its bounded connect returns. The connected socket is pinned, because a
    response that closes the connection detaches it from the connection while its body is still being read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._aborted = False
        self._connection: http.client.HTTPConnection | None = None
        self._socket: socket.socket | None = None

    def abort(self) -> None:
        # Shutting down under the lock keeps the request from detaching and closing the connection meanwhile.
        with self._lock:
            self._aborted = True
            sock = self._socket or getattr(self._connection, "sock", None)
            if sock is not None:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def pin(self, sock: socket.socket) -> None:
        """Keep the connected socket abortable for the whole response, even after the connection releases it."""
        with self._lock:
            self._socket = sock
        self.check()

    def attach(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            self._connection = connection
        self.check()

    def check(self) -> None:
        with self._lock:
            if self._aborted:
                raise BrainRuntimeError("Brain runtime request was stopped")

    def detach(self) -> None:
        with self._lock:
            self._connection = None
            self._socket = None


_ABORT: contextvars.ContextVar[RequestAbort | None] = contextvars.ContextVar("brain_request_abort", default=None)


@contextmanager
def abortable(handle: RequestAbort) -> Iterator[None]:
    """Let ``handle`` abort every Brain request this thread makes inside the block."""
    token = _ABORT.set(handle)
    try:
        yield
    finally:
        _ABORT.reset(token)


def _connection(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port, timeout=timeout)


@dataclass(frozen=True, slots=True)
class RouteCredentials:
    """One routing call's request-scoped model credential and optional TypeSafe decision key (ADR-0077)."""

    provider: Literal["anthropic", "openai"]
    model: str
    api_key: str
    decision_key: str | None = None


def _invalid_secret(value: object) -> bool:
    return not isinstance(value, str) or not 16 <= len(value) <= 8192 or not value.isascii() or "\0" in value


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
                "effort": context.effort,
            },
            "memories": None if context.memories is None else [dict(entry) for entry in context.memories],
            "skills": None if context.skills is None else [dict(skill) for skill in context.skills],
            "routines": None if context.routines is None else [dict(item) for item in context.routines],
            "knowledge_writable": context.knowledge_writable,
        }

    def _post(self, path: str, payload: Mapping[str, object], *, deadline: float | None = None) -> object:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        abort = _ABORT.get()
        connection = self._connection_factory(self._host, self._port, CONNECT_TIMEOUT_SECONDS)
        # An overall deadline shuts the socket down from a timer, like Stop, so Brain sees the disconnect.
        expiry = RequestAbort()
        timer = None if deadline is None else threading.Timer(deadline, expiry.abort)
        try:
            if timer is not None:
                expiry.attach(connection)
                timer.start()
            if abort is not None:
                abort.attach(connection)
            connection.connect()
            connected = connection.sock
            connected.settimeout(min(deadline or RESPONSE_TIMEOUT_SECONDS, RESPONSE_TIMEOUT_SECONDS))
            for handle in (abort, expiry):
                if handle is not None:
                    handle.pin(connected)
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
        except (OSError, http.client.HTTPException) as exc:
            raise BrainRuntimeError("Brain runtime is unavailable") from exc
        finally:
            if timer is not None:
                timer.cancel()
            expiry.detach()
            if abort is not None:
                abort.detach()
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
    def _metered(value: object, operation: str, provider: str, model: str) -> object:
        """Record the operation's reported model usage, even when the rest is invalid, and return the rest."""
        if not isinstance(value, dict) or "usage" not in value:
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        rest = dict(value)
        try:
            counts = brain_usage.parse(rest.pop("usage"))
        except brain_usage.UsageError as exc:
            raise BrainRuntimeError("Brain runtime returned an invalid response") from exc
        brain_usage.record(operation, provider, model, counts)
        return rest

    @staticmethod
    def _parse_routine(value: dict[str, object]) -> dict[str, object] | None:
        """A completed turn's one compiled Routine change, or the Routine question beside exactly its clarification.

        Local Team admits its shape.
        """
        routine = value["routine"]
        if routine is None:
            return None
        if (
            not isinstance(routine, dict)
            or value["status"] != "completed"
            or (value["clarification"] is None) == ("question" in routine)
        ):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        return routine

    @staticmethod
    def _parse_turn(value: object) -> RuntimeTurn:
        if not isinstance(value, dict) or set(value) != {
            "status",
            "reply",
            "actions",
            "clarification",
            "memory",
            "routine",
        }:
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        memory = http_payload.canonical_memory_changes(value["memory"])
        if memory is None or (memory and value["status"] != "completed"):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        routine = BrainRuntimeClient._parse_routine(value)
        clarification = value["clarification"]
        if clarification is not None:
            clarification = http_payload.canonical_clarification(clarification)
            if (
                clarification is None
                or value["status"] != "completed"
                or value["reply"] != http_payload.render_clarification(clarification)
            ):
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
        return RuntimeTurn(
            status=status,
            reply=reply,
            actions=tuple(actions),
            clarification=clarification,
            memory=tuple(memory),
            routine=routine,
        )

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
                and (
                    not allow_layout
                    or (unicodedata.category(character) != "Cf" and character not in _LANGUAGE_LAYOUT_CONTROLS)
                )
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

    @classmethod
    def _validate_lifecycle_reference(
        cls,
        value: RuntimeLifecycleReference | None,
    ) -> RuntimeLifecycleReference | None:
        if value is None:
            return None
        if not isinstance(value, RuntimeLifecycleReference) or ACTION_ID_RE.fullmatch(value.id) is None:
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        return RuntimeLifecycleReference(
            value.id,
            cls._capability_text(value.name, MAX_INTENT_ROUTE_NAME_CHARS),
        )

    @classmethod
    def _validate_lifecycle_context(
        cls,
        value: RuntimeLifecycleContext | None,
        expected_intent: LifecycleIntent | None,
    ) -> RuntimeLifecycleContext:
        if not isinstance(value, RuntimeLifecycleContext) or http_payload.canonical_locale(value.locale) is None:
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        reference = cls._validate_lifecycle_reference(value.reference)
        conversation = cls.validate_conversation(value.conversation)
        if expected_intent is not None and (reference is not None or conversation):
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        return RuntimeLifecycleContext(reference, conversation, value.locale)

    @classmethod
    def validate_conversation(
        cls,
        value: object,
    ) -> tuple[RuntimeConversationEntry, ...]:
        """Admit one bounded window of untrusted committed presentation history."""
        if not isinstance(value, tuple) or len(value) > MAX_CONVERSATION_ENTRIES:
            raise BrainRuntimeError("Brain runtime conversation window is invalid")
        admitted: list[RuntimeConversationEntry] = []
        for entry in value:
            if (
                not isinstance(entry, RuntimeConversationEntry)
                or entry.role not in {"user", "assistant"}
                or not isinstance(entry.truncated, bool)
            ):
                raise BrainRuntimeError("Brain runtime conversation window is invalid")
            admitted.append(
                RuntimeConversationEntry(
                    entry.role,
                    cls._capability_text(entry.text, MAX_CONVERSATION_TEXT_CHARS, allow_layout=True),
                    entry.truncated,
                )
            )
        result = tuple(admitted)
        if sum(len(entry.text) for entry in result) > MAX_CONVERSATION_CHARS:
            raise BrainRuntimeError("Brain runtime conversation window is invalid")
        return result

    @classmethod
    def validate_intent_route_inputs(
        cls,
        objective: object,
        expected_intent: LifecycleIntent | None,
        candidates: tuple[RuntimeDirectoryCandidate, ...],
        context: RuntimeLifecycleContext | None,
    ) -> tuple[
        str,
        LifecycleIntent | None,
        tuple[RuntimeDirectoryCandidate, ...],
        RuntimeLifecycleContext,
    ]:
        task = cls._capability_text(objective, MAX_CAPABILITY_OBJECTIVE_CHARS, allow_layout=True)
        admitted_context = cls._validate_lifecycle_context(context, expected_intent)
        if expected_intent is None:
            if candidates != ():
                raise BrainRuntimeError("Brain runtime intent route request is invalid")
            return task, None, (), admitted_context
        if expected_intent not in {"assistant-install", "assistant-uninstall"}:
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        if not isinstance(candidates, tuple) or len(candidates) > MAX_INTENT_ROUTE_CANDIDATES:
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        admitted: list[RuntimeDirectoryCandidate] = []
        for candidate in candidates:
            if not isinstance(candidate, RuntimeDirectoryCandidate) or ACTION_ID_RE.fullmatch(candidate.id) is None:
                raise BrainRuntimeError("Brain runtime intent route request is invalid")
            name = cls._capability_text(candidate.name, MAX_INTENT_ROUTE_NAME_CHARS)
            summary = candidate.summary
            if not isinstance(summary, str) or len(summary) > MAX_INTENT_ROUTE_SUMMARY_CHARS:
                raise BrainRuntimeError("Brain runtime intent route request is invalid")
            if summary:
                summary = cls._capability_text(summary, MAX_INTENT_ROUTE_SUMMARY_CHARS)
            if expected_intent == "assistant-uninstall" and summary:
                raise BrainRuntimeError("Brain runtime intent route request is invalid")
            admitted.append(RuntimeDirectoryCandidate(candidate.id, name, summary))
        result = tuple(admitted)
        if tuple(item.id for item in result) != tuple(sorted({item.id for item in result})):
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        return task, expected_intent, result, admitted_context

    @staticmethod
    def _parse_intent_route(
        value: object,
        expected_intent: LifecycleIntent | None,
        candidates: tuple[RuntimeDirectoryCandidate, ...],
    ) -> RuntimeIntentRoute:
        if not isinstance(value, dict) or set(value) != {"intent", "query", "assistant_ids", "reply", "task_follows"}:
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        task_follows = value["task_follows"]
        continues_install = expected_intent is None and value["intent"] == "assistant-install" and bool(value["query"])
        if type(task_follows) is not bool or (task_follows and not continues_install):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        intent = value["intent"]
        query = value["query"]
        raw_ids = value["assistant_ids"]
        reply = value["reply"]
        if (
            intent not in {"ordinary-task", "assistant-install", "assistant-uninstall", "unresolved"}
            or not isinstance(query, str)
            or query.strip() != query
            or len(query) > MAX_INTENT_ROUTE_QUERY_CHARS
            or any(unicodedata.category(character).startswith("C") for character in query)
            or not isinstance(raw_ids, list)
            or any(not isinstance(item, str) for item in raw_ids)
            or not isinstance(reply, str)
            or reply.strip() != reply
            or len(reply) > MAX_INTENT_ROUTE_REPLY_CHARS
            or any(
                unicodedata.category(character).startswith("C") or unicodedata.category(character) in {"Zl", "Zp"}
                for character in reply
            )
        ):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        assistant_ids = tuple(raw_ids)
        if assistant_ids != tuple(sorted(set(assistant_ids))):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        if expected_intent is None:
            if assistant_ids or (intent in {"ordinary-task", "unresolved"} and query):
                raise BrainRuntimeError("Brain runtime returned an invalid response")
            requires_reply = intent == "unresolved" or (
                intent in {"assistant-install", "assistant-uninstall"} and not query
            )
            if bool(reply) != requires_reply:
                raise BrainRuntimeError("Brain runtime returned an invalid response")
            return RuntimeIntentRoute(intent, query, reply=reply, task_follows=task_follows)
        expected_ids = frozenset(candidate.id for candidate in candidates)
        if intent == "unresolved" and not query and not assistant_ids and reply:
            return RuntimeIntentRoute("unresolved", reply=reply)
        if (
            intent != expected_intent
            or query
            or not assistant_ids
            or len(assistant_ids) > MAX_INTENT_ROUTE_SELECTED
            or any(item not in expected_ids for item in assistant_ids)
            or (expected_intent == "assistant-uninstall" and len(assistant_ids) != 1)
            or reply
        ):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        return RuntimeIntentRoute(intent, assistant_ids=assistant_ids)

    def start(
        self,
        context: RuntimeContext,
        message: str,
        *,
        conversation: tuple[RuntimeConversationEntry, ...],
    ) -> RuntimeTurn:
        """Start a turn; the Brain uses ``conversation`` only when it retains no completed exchange."""
        if context.locale is not None and http_payload.canonical_locale(context.locale) is None:
            raise BrainRuntimeError("Brain runtime turn locale is invalid")
        payload = self._context(context)
        payload["message"] = message
        payload["locale"] = context.locale
        payload["conversation"] = [
            {"role": entry.role, "text": entry.text, "truncated": entry.truncated}
            for entry in self.validate_conversation(conversation)
        ]
        response = self._post("/v1/turns", payload)
        return self._parse_turn(self._metered(response, "turn", context.provider, context.model))

    def resume(self, context: RuntimeContext, results: Mapping[str, object]) -> RuntimeTurn:
        payload = self._context(context)
        payload["results"] = dict(results)
        response = self._post("/v1/turns/resume", payload)
        return self._parse_turn(self._metered(response, "turn-resume", context.provider, context.model))

    def purpose(self, context: RuntimeContext, request: ActionRequest, assistant_name: str, summary: str) -> str | None:
        """Ask once why the user's task needs this paused Action, in the turn's language; any failure is None.

        Brain reads only the pending turn's own message for this exact interrupt (ADR-0090). Stop aborts the request
        like any other; the caller checks for Stop afterwards.
        """
        payload = {
            "thread_id": context.thread_id,
            "interrupt_id": request.interrupt_id,
            "assistant_id": request.assistant_id,
            "assistant_name": assistant_name,
            "action_id": request.action,
            "action_summary": summary,
            "provider": {"provider": context.provider, "model": context.model, "api_key": context.api_key},
        }
        try:
            response = self._post("/v1/turns/purpose", payload, deadline=PURPOSE_DEADLINE_SECONDS)
            rest = self._metered(response, "purpose", context.provider, context.model)
        except BrainRuntimeError:
            return None
        if not isinstance(rest, dict) or set(rest) != {"purpose"} or rest["purpose"] is None:
            return None
        return http_payload.canonical_purpose(rest["purpose"])

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
        locale: str,
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
            or http_payload.canonical_locale(locale) is None
            or not 1 <= len(action_ids) <= MAX_ACTION_LABELS
            or any(ACTION_ID_RE.fullmatch(action_id) is None for action_id in action_ids)
            or len(set(action_ids)) != len(action_ids)
        ):
            raise BrainRuntimeError("Brain runtime Action label request is invalid")
        response = self._post(
            "/v1/action-labels",
            {
                "provider": {"provider": provider, "model": model, "api_key": api_key},
                "locale": locale,
                "actions": list(action_ids),
            },
        )
        return self._parse_action_labels(self._metered(response, "action-labels", provider, model), action_ids)

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
        return self._parse_capability_plan(self._metered(response, "capability-plan", provider, model), admitted)

    def intent_route(
        self,
        *,
        credentials: RouteCredentials,
        objective: object,
        expected_intent: LifecycleIntent | None,
        candidates: tuple[RuntimeDirectoryCandidate, ...],
        context: RuntimeLifecycleContext | None,
    ) -> RuntimeIntentRoute:
        if not isinstance(credentials, RouteCredentials):
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        provider, model, api_key, decision_key = (
            credentials.provider,
            credentials.model,
            credentials.api_key,
            credentials.decision_key,
        )
        if (
            provider not in {"anthropic", "openai"}
            or not isinstance(model, str)
            or SAFE_ID_RE.fullmatch(model) is None
            or not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 16 * 1024
            or "\0" in api_key
            or (decision_key is not None and (expected_intent is not None or _invalid_secret(decision_key)))
        ):
            raise BrainRuntimeError("Brain runtime intent route request is invalid")
        task, expected, admitted, admitted_context = self.validate_intent_route_inputs(
            objective,
            expected_intent,
            candidates,
            context,
        )
        response = self._post(
            "/v1/intent-route",
            {
                "provider": {"provider": provider, "model": model, "api_key": api_key},
                "objective": task,
                "expected_intent": expected,
                "candidates": [
                    {"id": candidate.id, "name": candidate.name, "summary": candidate.summary} for candidate in admitted
                ],
                "lifecycle_reference": (
                    None
                    if admitted_context.reference is None
                    else {"id": admitted_context.reference.id, "name": admitted_context.reference.name}
                ),
                "conversation": [
                    {"role": entry.role, "text": entry.text, "truncated": entry.truncated}
                    for entry in admitted_context.conversation
                ],
                "locale": admitted_context.locale,
                **(
                    {}
                    if decision_key is None
                    else {"decision_provider": {"provider": "typesafe", "api_key": decision_key}}
                ),
            },
        )
        return self._parse_intent_route(self._metered(response, "intent-route", provider, model), expected, admitted)
