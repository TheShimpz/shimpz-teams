"""Shared fail-closed Action execution primitives for Hosted and Local."""

from __future__ import annotations

import hashlib
import json
import select
import socket
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import NoReturn

from action import human as action_human
from action import journal as action_journal
from action import stored_input as action_stored_input
from core import strict_json

# A missing manifest Action is a missing resource; an unavailable connected integration is an unmet
# request precondition. Both Controllers use these statuses so their public contracts cannot drift.
UNDECLARED_ACTION_STATUS = HTTPStatus.NOT_FOUND
INTEGRATION_PRECONDITION_STATUS = HTTPStatus.PRECONDITION_REQUIRED
RPC_FAILURE_STATUSES = {
    "timeout": HTTPStatus.GATEWAY_TIMEOUT,
    "ambiguous": HTTPStatus.BAD_GATEWAY,
    "invalid-result": HTTPStatus.BAD_GATEWAY,
    "failed": HTTPStatus.BAD_GATEWAY,
}
RPC_FAILURE_MESSAGES = {
    "timeout": ("Assistant Action timed out", "assistant-timeout"),
    "ambiguous": ("Assistant Action status is ambiguous", "assistant-rpc-failed"),
    "invalid-result": ("Assistant Action returned an invalid result", "assistant-rpc-failed"),
    "failed": ("Assistant Action failed", "assistant-rpc-failed"),
}
ACTION_COMMAND = "/usr/local/bin/shimpz-action"
RPC_TIMEOUT_SECONDS = 8
MAX_RPC_RESPONSE_BYTES = 512 * 1024
MAX_RPC_REQUEST_BYTES = 512 * 1024
ASSISTANT_RPC_USER = "10001:10001"


def _raise_unknown_rpc_failure(kind: str) -> NoReturn:
    raise AssertionError(f"unknown RPC failure: {kind}")


def rpc_failure_status(kind: str) -> HTTPStatus:
    """Map every non-routing RPC failure kind to its shared HTTP status."""
    try:
        return RPC_FAILURE_STATUSES[kind]
    except KeyError:
        _raise_unknown_rpc_failure(kind)


def rpc_failure_message(kind: str) -> tuple[str, str]:
    """Map every non-routing RPC failure kind to its shared public message."""
    try:
        return RPC_FAILURE_MESSAGES[kind]
    except KeyError:
        _raise_unknown_rpc_failure(kind)


def integration_access_tokens(integrations: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    """Project controller integration records into the minimal Spec v1 token mapping."""
    tokens: dict[str, str] = {}
    for integration_id, envelope in integrations.items():
        if (
            not isinstance(integration_id, str)
            or set(envelope) != {"type", "access_token"}
            or envelope["type"] != "oauth2-bearer"
            or not isinstance(envelope["access_token"], str)
        ):
            raise ValueError("Assistant integration envelope is invalid")
        tokens[integration_id] = envelope["access_token"]
    return tokens


def encode_rpc_invocation(
    action_input: object,
    integrations: Mapping[str, str],
    stored_inputs: Mapping[str, str],
    responses: tuple[Mapping[str, object], ...] = (),
) -> bytes:
    """Encode one bounded Spec v1 invocation, adding responses only for replay."""
    invocation: dict[str, object] = {
        "input": action_input,
        "integrations": dict(integrations),
        "stored_inputs": dict(stored_inputs),
    }
    if responses:
        invocation["responses"] = [dict(response) for response in responses]
    try:
        encoded = json.dumps(
            invocation,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError("Assistant Action invocation is invalid") from exc
    if len(encoded) > MAX_RPC_REQUEST_BYTES:
        raise ValueError("Assistant Action invocation is too large")
    return encoded


def action_operation(
    request: object,
    assistant_container_id: object,
    assistant_image: object,
    integration_generations: tuple[tuple[str, int], ...] = (),
    stored_input_generations: tuple[tuple[str, int], ...] = (),
) -> action_journal.Operation:
    """Fingerprint one normalized request and every immutable private-state generation."""
    if not isinstance(assistant_container_id, str) or not assistant_container_id:
        raise action_journal.ActionJournalConflictError("Assistant generation is invalid")
    if not isinstance(assistant_image, str) or not assistant_image:
        raise action_journal.ActionJournalConflictError("Assistant generation is invalid")
    try:
        encoded = json.dumps(
            {
                "assistant_container_id": assistant_container_id,
                "assistant_id": request.assistant_id,
                "assistant_image": assistant_image,
                "integration_generations": integration_generations,
                "stored_input_generations": stored_input_generations,
                "input": request.input,
                "action": request.action,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise action_journal.ActionJournalConflictError("Action request cannot be fingerprinted") from exc
    return action_journal.Operation(request.interrupt_id, hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True, slots=True)
class ActionBatchStrategy:
    binding_identity: Callable[[object], tuple[object, object]]
    execute: Callable[[object, object], object]
    preflight: Callable[[object], object]
    integration_generations: Callable[[object], tuple[tuple[str, int], ...]] = lambda _request: ()
    stored_input_generations: Callable[[object], tuple[tuple[str, int], ...]] = lambda _request: ()


class ActionBatch:
    """Bind a Brain suspension to one durable journal batch and immutable workload identities."""

    def __init__(
        self,
        journal: action_journal.ActionJournal | Callable[[], action_journal.ActionJournal],
        generation: str,
        thread_id: str,
        bindings: Mapping[str, object],
        strategy: ActionBatchStrategy,
    ) -> None:
        self._journal_source = journal
        self._journal = journal if isinstance(journal, action_journal.ActionJournal) else None
        self._generation = generation
        self._thread_id = thread_id
        self._bindings = bindings
        self._strategy = strategy
        self._batch: action_journal.Batch | None = None
        self._operations: dict[str, action_journal.Operation] = {}
        self._executing_here: set[str] = set()

    def _operation_with_evidence(self, request: object) -> tuple[action_journal.Operation, object]:
        active = self._bindings.get(request.assistant_id)
        if active is None:
            raise action_journal.ActionJournalConflictError("Action Assistant is unavailable")
        evidence = self._strategy.preflight(request)
        container_id, image = self._strategy.binding_identity(active)
        return (
            action_operation(
                request,
                container_id,
                image,
                self._strategy.integration_generations(request),
                self._strategy.stored_input_generations(request),
            ),
            evidence,
        )

    def _operation(self, request: object) -> action_journal.Operation:
        return self._operation_with_evidence(request)[0]

    def prepare(self, requests: tuple[object, ...]) -> None:
        if self._batch is not None:
            raise action_journal.ActionJournalConflictError("Action batch is already prepared")
        operations = tuple(self._operation(request) for request in requests)
        if self._journal is None:
            self._journal = self._journal_source()
        self._batch = self._journal.prepare_batch(self._generation, self._thread_id, operations)
        self._operations = {operation.interrupt_id: operation for operation in operations}

    def invoke(self, request: object) -> object:
        if self._journal is None or self._batch is None:
            raise action_journal.ActionJournalConflictError("Action batch is not prepared")
        operation = self._operations.get(request.interrupt_id)
        if operation is None:
            raise action_journal.ActionJournalConflictError("Action operation is not prepared")
        current_operation, evidence = self._operation_with_evidence(request)
        if current_operation != operation:
            raise action_journal.ActionJournalConflictError("Action credential generation changed")
        decision = self._journal.begin(self._batch, operation)
        if not decision.execute:
            return decision.result
        self._executing_here.add(operation.interrupt_id)
        try:
            result = self._strategy.execute(request, evidence)
        except action_human.HumanRequestSuspensionError:
            self._journal.suspend(self._batch, operation)
            self._executing_here.discard(operation.interrupt_id)
            raise
        self._journal.complete(self._batch, operation, result)
        self._executing_here.discard(operation.interrupt_id)
        return result

    def delivered(self, requests: tuple[object, ...]) -> None:
        if self._journal is None or self._batch is None:
            raise action_journal.ActionJournalConflictError("Action batch is not prepared")
        expected = tuple(operation.interrupt_id for operation in self._batch.operations)
        if tuple(request.interrupt_id for request in requests) != expected:
            raise action_journal.ActionJournalConflictError("Action delivery batch changed")
        self._journal.delivered(self._batch)
        self._batch = None
        self._operations = {}
        self._executing_here.clear()
        if callable(self._journal_source):
            self._journal = None

    def abandon_uncertain(self) -> bool:
        """Release only this batch after an in-band terminal uncertain execution."""
        if self._journal is None or self._batch is None or not self._executing_here:
            return False
        abandoned = self._journal.abandon_uncertain(self._batch)
        if abandoned:
            self._batch = None
            self._operations = {}
            self._executing_here.clear()
            if callable(self._journal_source):
                self._journal = None
        return abandoned


class RpcExchangeError(RuntimeError):
    """One stable failure kind translated into each Controller's public error shape."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class RpcSecretExposureError(ValueError):
    """An Assistant returned a literal private value."""


class RpcInvalidResultError(ValueError):
    """An Assistant result failed its reviewed Action schema."""


class StoredInputRejectedError(RuntimeError):
    """An Assistant explicitly rejected one exact supplied persistent input."""

    def __init__(self, stored_input: str) -> None:
        super().__init__("Assistant rejected one Stored Input")
        self.stored_input = stored_input


@dataclass(frozen=True, slots=True, repr=False)
class RpcResultPolicy:
    """Reviewed Action result capabilities and private values for one invocation."""

    human_requests: tuple[str, ...] = ()
    protected_values: Mapping[str, str] | None = None
    authorization_requested: bool = False
    stored_inputs_by_id: Mapping[str, str] | None = None
    declared_stored_inputs: tuple[str, ...] = ()
    supplied_stored_inputs: frozenset[str] = frozenset()


_DEFAULT_RPC_RESULT_POLICY = RpcResultPolicy()


def project_rpc_result(
    raw_result: object,
    integrations_by_id: Mapping[str, Mapping[str, object]],
    validate: Callable[[object], object],
    policy: RpcResultPolicy = _DEFAULT_RPC_RESULT_POLICY,
) -> object:
    """Reject private echoes, validate one tagged result, or raise one admitted suspension."""
    secrets = protected_rpc_values(integrations_by_id)
    if policy.protected_values is not None:
        secrets.update(policy.protected_values)
    if policy.stored_inputs_by_id is not None:
        secrets.update({f"stored-input:{key}": value for key, value in policy.stored_inputs_by_id.items()})
    if contains_secret(raw_result, secrets):
        raise RpcSecretExposureError
    valid_fields = ({"type", "result"}, {"type", "request"}, {"type", "stored_input"})
    if not isinstance(raw_result, dict) or set(raw_result) not in valid_fields:
        raise RpcInvalidResultError
    response_type = raw_result.get("type")
    if response_type == "stored_input_rejected":
        rejected = raw_result.get("stored_input")
        if rejected not in policy.declared_stored_inputs or rejected not in policy.supplied_stored_inputs:
            raise RpcInvalidResultError
        raise StoredInputRejectedError(str(rejected))
    if response_type == "request" and "request" in raw_result:
        try:
            request = action_human.validate_request(
                raw_result["request"],
                policy.human_requests,
                policy.declared_stored_inputs,
            )
        except action_human.HumanRequestError as exc:
            raise RpcInvalidResultError from exc
        if policy.authorization_requested and request.kind in action_human.AUTHORIZATION_KINDS:
            raise RpcInvalidResultError
        raise action_human.HumanRequestSuspensionError(request)
    if response_type != "result" or "result" not in raw_result:
        raise RpcInvalidResultError
    try:
        return validate(raw_result["result"])
    except ValueError as exc:
        raise RpcInvalidResultError from exc


def decode_rpc_response(raw: bytes) -> dict[str, object]:
    """Decode one direct Spec v1 Action result."""
    try:
        response = strict_json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RpcExchangeError("invalid-result") from exc
    if not isinstance(response, dict):
        raise RpcExchangeError("invalid-result")
    return response


@dataclass(frozen=True, slots=True)
class RpcExchangeStrategy:
    api: object
    user: str
    workdir: str
    timeout: float
    maximum: int
    transport_errors: tuple[type[BaseException], ...]
    fail_stop: Callable[[], None]
    cancelled: Callable[[BaseException | None], None]
    close_stream: Callable[[object], None]


def rpc_exchange(
    container_id: str,
    argv: list[str],
    encoded: bytes,
    strategy: RpcExchangeStrategy,
    *,
    detect_unsupported_path: bool = False,
) -> object:
    """Execute one bounded Docker RPC with shared fail-stop and framing decisions."""
    transport_errors = strategy.transport_errors
    try:
        # Docker exec Env is additive; the workload inherits the container environment intentionally.
        created = strategy.api.exec_create(
            container_id,
            argv,
            stdin=True,
            stdout=True,
            stderr=True,
            privileged=False,
            user=strategy.user,
            workdir=strategy.workdir,
        )
        exec_id = created["Id"]
        stream = strategy.api.exec_start(exec_id, socket=True)
        if stream is None:
            raise OSError("Docker attach stream is unavailable")
        try:
            raw_socket = getattr(stream, "_sock", None)
            if raw_socket is None:
                raise OSError("Docker attach socket cannot half-close stdin")
            deadline = time.monotonic() + strategy.timeout
            _write_all(raw_socket, encoded, deadline)
            raw_socket.shutdown(socket.SHUT_WR)
            stdout, stderr = read_rpc_frames(raw_socket, deadline, strategy.maximum)
        finally:
            strategy.close_stream(stream)
    except TimeoutError as exc:
        strategy.fail_stop()
        strategy.cancelled(exc)
        raise RpcExchangeError("timeout") from exc
    except (*transport_errors, OSError, ValueError, KeyError) as exc:
        strategy.fail_stop()
        strategy.cancelled(exc)
        raise RpcExchangeError("failed") from exc

    try:
        details = strategy.api.exec_inspect(exec_id)
    except transport_errors as exc:
        strategy.fail_stop()
        strategy.cancelled(exc)
        raise RpcExchangeError("ambiguous") from exc
    exit_code = details.get("ExitCode")
    if not isinstance(exit_code, int):
        strategy.fail_stop()
        strategy.cancelled(None)
        raise RpcExchangeError("ambiguous")
    if exit_code != 0 or stderr:
        if detect_unsupported_path and exit_code == 2 and not stdout and not stderr:
            raise RpcExchangeError("unsupported-path")
        strategy.cancelled(None)
        raise RpcExchangeError("failed")
    return decode_rpc_response(bytes(stdout))


def private_generations(metadata: tuple[object, ...]) -> tuple[tuple[str, int], ...]:
    """Project only usable positive Integration generations."""
    valid = all(getattr(item, "status", None) == "connected" for item in metadata)
    generations = tuple(getattr(item, "generation", None) for item in metadata)
    if not valid or any(type(generation) is not int or generation < 1 for generation in generations):
        raise action_journal.ActionJournalConflictError("Action integration generation is unavailable")
    return tuple((item.id, generation) for item, generation in zip(metadata, generations, strict=True))


def integration_generations(
    actions: Mapping[str, object],
    integrations: Mapping[str, object],
    action_id: str,
    metadata: Callable[[dict[str, object]], tuple[object, ...]],
) -> tuple[tuple[str, int], ...]:
    """Read one declared Action's connected integration generations."""
    action = actions.get(action_id)
    if action is None:
        raise action_journal.ActionJournalConflictError("Action integration contract is unavailable")
    integration_ids = tuple(getattr(action, "integrations", ()))
    declarations = {
        integration_id: integrations[integration_id]
        for integration_id in integration_ids
        if integration_id in integrations
    }
    if len(declarations) != len(integration_ids):
        raise action_journal.ActionJournalConflictError("Action integration contract is unavailable")
    return private_generations(tuple(metadata(declarations)))


def stored_input_origin(request: object) -> str:
    """Bind a newly consumed Stored Input to one exact Brain Action interrupt."""
    try:
        encoded = json.dumps(
            {
                "action": request.action,
                "assistant_id": request.assistant_id,
                "input": request.input,
                "interrupt_id": request.interrupt_id,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (AttributeError, TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise action_journal.ActionJournalConflictError("Action Stored Input origin is invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _stored_input_declarations(
    actions: Mapping[str, object],
    stored_inputs: Mapping[str, object],
    action_id: str,
) -> dict[str, object]:
    action = actions.get(action_id)
    if action is None:
        raise action_journal.ActionJournalConflictError("Action Stored Input contract is unavailable")
    stored_input_ids = tuple(getattr(action, "stored_inputs", ()))
    declarations = {
        stored_input_id: stored_inputs[stored_input_id]
        for stored_input_id in stored_input_ids
        if stored_input_id in stored_inputs
    }
    if len(declarations) != len(stored_input_ids):
        raise action_journal.ActionJournalConflictError("Action Stored Input contract is unavailable")
    return declarations


def resolve_action_stored_inputs(
    actions: Mapping[str, object],
    stored_inputs: Mapping[str, object],
    action_id: str,
    resolve: Callable[[str, object], action_stored_input.StoredInputValue],
) -> dict[str, action_stored_input.StoredInputValue]:
    """Resolve only the current Action's exact declared persistent input."""
    values: dict[str, action_stored_input.StoredInputValue] = {}
    for stored_input_id, declaration in _stored_input_declarations(actions, stored_inputs, action_id).items():
        try:
            value = resolve(stored_input_id, declaration)
        except action_stored_input.StoredInputMissingError:
            continue
        if not isinstance(value, action_stored_input.StoredInputValue):
            raise action_journal.ActionJournalConflictError("Action Stored Input state is unavailable")
        values[stored_input_id] = value
    return values


def stored_input_generations(
    actions: Mapping[str, object],
    stored_inputs: Mapping[str, object],
    action_id: str,
    origin: str,
    resolve: Callable[[str, object], action_stored_input.StoredInputValue],
) -> tuple[tuple[str, int], ...]:
    """Fingerprint reusable generations while preserving a just-sealed journal replay."""
    values = resolve_action_stored_inputs(actions, stored_inputs, action_id, resolve)
    generations: list[tuple[str, int]] = []
    for stored_input_id, value in values.items():
        if value.origin == origin:
            continue
        if type(value.generation) is not int or value.generation < 1:
            raise action_journal.ActionJournalConflictError("Action Stored Input generation is unavailable")
        generations.append((stored_input_id, value.generation))
    return tuple(generations)


@dataclass(frozen=True, slots=True, repr=False)
class RpcPrivateInputs:
    """Exact private values frozen by one invoke-time preflight."""

    integrations: Mapping[str, Mapping[str, object]]
    stored_inputs: Mapping[str, str]


@dataclass(frozen=True, slots=True, repr=False)
class ActionInvocationEvidence:
    """Invoke-time private evidence plus the memory-only replay transcript."""

    private_inputs: RpcPrivateInputs
    transcript: action_human.ActionTranscript
    origin: str


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedInvocationEvidence:
    """Private values selected for an invocation, including direct calls without replay."""

    integrations: Mapping[str, Mapping[str, object]]
    stored_inputs: Mapping[str, str]
    transcript: action_human.ActionTranscript
    origin: str | None


def resolve_invocation_evidence(
    evidence: ActionInvocationEvidence | None,
    resolve_integrations: Callable[[], Mapping[str, Mapping[str, object]]],
    resolve_stored_inputs: Callable[[], Mapping[str, action_stored_input.StoredInputValue]],
) -> ResolvedInvocationEvidence:
    """Use frozen chat evidence or resolve exact private values for a direct invocation."""
    if evidence is not None:
        return ResolvedInvocationEvidence(
            evidence.private_inputs.integrations,
            evidence.private_inputs.stored_inputs,
            evidence.transcript,
            evidence.origin,
        )
    resolved = resolve_stored_inputs()
    return ResolvedInvocationEvidence(
        resolve_integrations(),
        {stored_input_id: value.value for stored_input_id, value in resolved.items()},
        action_human.ActionTranscript(""),
        None,
    )


def require_rpc_envelope(
    active: object,
    request: object,
    resolve_integrations: Callable[[object, str], Mapping[str, Mapping[str, object]]],
    resolve_stored_inputs: Callable[[object, str], Mapping[str, action_stored_input.StoredInputValue]],
) -> RpcPrivateInputs:
    """Resolve and size-check the exact Spec v1 invocation before journaling."""
    integrations = resolve_integrations(active, request.action)
    resolved_stored_inputs = resolve_stored_inputs(active, request.action)
    stored_inputs = {stored_input_id: resolved.value for stored_input_id, resolved in resolved_stored_inputs.items()}
    encode_rpc_invocation(
        request.input,
        integration_access_tokens(integrations),
        stored_inputs,
    )
    return RpcPrivateInputs(integrations, stored_inputs)


def contains_secret(value: object, secrets_by_id: Mapping[str, str]) -> bool:
    """Fail closed on literal secret echoes or inputs nested beyond the inspection bound."""
    secret_values = tuple(secret for secret in secrets_by_id.values() if secret)

    def visit(item: object, depth: int = 0) -> bool:
        if depth > 32:
            return True
        if isinstance(item, str):
            return any(secret in item for secret in secret_values)
        if isinstance(item, list | tuple):
            return any(visit(child, depth + 1) for child in item)
        if isinstance(item, dict):
            return any(visit(key, depth + 1) or visit(child, depth + 1) for key, child in item.items())
        return False

    return bool(secret_values) and visit(value)


def protected_rpc_values(
    integrations_by_id: Mapping[str, Mapping[str, object]],
) -> dict[str, str]:
    """Collect literal integration tokens that an Assistant must not return."""
    return {
        f"integration:{integration_id}": access_token
        for integration_id, envelope in integrations_by_id.items()
        if isinstance((access_token := envelope.get("access_token")), str)
    }


def _read_exact(raw_socket: socket.socket, amount: int, deadline: float) -> bytes:
    output = bytearray()
    while len(output) < amount:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([raw_socket], [], [], remaining)[0]:
            raise TimeoutError
        chunk = raw_socket.recv(amount - len(output))
        if not chunk:
            raise EOFError
        output.extend(chunk)
    return bytes(output)


def _write_all(raw_socket: socket.socket, data: bytes, deadline: float) -> None:
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [raw_socket], [], remaining)[1]:
            raise TimeoutError
        sent += raw_socket.send(view[sent:])


def read_rpc_frames(raw_socket: socket.socket, deadline: float, maximum: int) -> tuple[bytes, bytes]:
    """Read Docker's multiplexed exec frames with one shared bounded parser."""
    stdout = bytearray()
    stderr = bytearray()
    while True:
        try:
            first = _read_exact(raw_socket, 1, deadline)
        except EOFError:
            break
        try:
            header = first + _read_exact(raw_socket, 7, deadline)
        except EOFError as exc:
            raise ValueError("truncated Assistant RPC frame header") from exc
        stream_id, length = struct.unpack(">BxxxL", header)
        if stream_id not in {1, 2}:
            raise ValueError("invalid Assistant RPC stream")
        if length > maximum + 1:
            raise ValueError("oversized Assistant RPC frame")
        try:
            chunk = _read_exact(raw_socket, length, deadline)
        except EOFError as exc:
            raise ValueError("truncated Assistant RPC frame payload") from exc
        target = stdout if stream_id == 1 else stderr
        target.extend(chunk)
        if len(stdout) + len(stderr) > maximum:
            raise ValueError("oversized Assistant RPC response")
    return bytes(stdout), bytes(stderr)


def close_exec_stream(stream: object) -> None:
    """Close docker-py's owning HTTP response before its raw socket."""
    response = getattr(stream, "_response", None)
    if response is not None:
        response.close()
    else:
        stream.close()
