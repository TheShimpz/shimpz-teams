"""Shared fail-closed Action execution primitives for Hosted and Local."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import json
import select
import socket
import struct
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass
from http import HTTPStatus
from typing import NoReturn

from action import failure as action_failure
from action import files as action_files
from action import human as action_human
from action import journal as action_journal
from action import stored_input as action_stored_input
from core import strict_json
from protocol.assistant.v1.validators import input_file as input_file_validator

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
MAX_RPC_REQUEST_BYTES = input_file_validator.MAX_INVOCATION_BYTES
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


# Sizes an invocation before its journal mints the real id, which always has this exact length.
OPERATION_ID_PLACEHOLDER = "00000000-0000-4000-8000-000000000000"


def encode_rpc_invocation(
    action_input: object,
    integrations: Mapping[str, str],
    stored_inputs: Mapping[str, str],
    operation_id: str,
    responses: tuple[Mapping[str, object], ...] = (),
    files: Mapping[str, object] | None = None,
) -> bytes:
    """Encode one bounded Spec v1 invocation of one logical operation, adding responses only for replay.

    ``files`` is always present, ``{}`` for an ordinary Action; only an invocation carrying delivered file content
    admits the larger bound the SDK applies to it (ADR-0093).
    """
    if not action_journal.valid_operation_id(operation_id):
        raise ValueError("Assistant Action operation id is invalid")
    invocation: dict[str, object] = {
        "input": action_input,
        "integrations": dict(integrations),
        "stored_inputs": dict(stored_inputs),
        "files": dict(files or {}),
        "operation_id": operation_id,
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
    delivers = input_file_validator.delivers_content(invocation)
    if len(encoded) > (input_file_validator.MAX_FILE_INVOCATION_BYTES if delivers else MAX_RPC_REQUEST_BYTES):
        raise ValueError("Assistant Action invocation is too large")
    return encoded


def action_operation(
    request: object,
    assistant_container_id: object,
    assistant_image: object,
    integration_generations: tuple[tuple[str, int], ...] = (),
    stored_input_generations: tuple[tuple[str, int], ...] = (),
    files: list[dict[str, object]] | None = None,
) -> action_journal.Operation:
    """Fingerprint one normalized request, every immutable private-state generation, and its file commitments.

    File commitments are always part of the preimage, ``[]`` for an ordinary Action (ADR-0093).
    """
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
                "files": files or [],
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
    # Runs one request with its preflight evidence under the logical operation id its journal minted.
    execute: Callable[[object, object, str], object]
    preflight: Callable[[object], object]
    integration_generations: Callable[[object], tuple[tuple[str, int], ...]] = lambda _request: ()
    stored_input_generations: Callable[[object, frozenset[str]], tuple[tuple[str, int], ...]] = (
        lambda _request, _origins: ()
    )
    # The pinned reviewed effect class of a request's Action; anything not declared read_only is mutating.
    effect: Callable[[object], str] = lambda _request: "mutating"
    # The logical operation a permitted Routine retry repeats (ADR-0092); None lets the journal mint a new one.
    operation_id: Callable[[object], str | None] = lambda _request: None
    # Admits one request's execution before its attempt is journaled: a file delivery holds the one file-RPC slot
    # here, so a refused or stopped wait leaves the journal unchanged (ADR-0093).
    admit: Callable[[object, object], AbstractContextManager[None]] = lambda _request, _evidence: nullcontext()


class ActionBatch:
    """Bind a Brain suspension to one durable journal batch and immutable workload identities."""

    # A Routine batch reserves the archive marker its incident may need before any of its Actions runs.
    archivable = False

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
        self._origins: frozenset[str] = frozenset()
        self._prepared_stored_inputs: dict[str, dict[str, int]] = {}

    def _operation_with_evidence(
        self,
        request: object,
        excluded_origins: frozenset[str] | None = None,
    ) -> tuple[action_journal.Operation, object]:
        active = self._bindings.get(request.assistant_id)
        if active is None:
            raise action_journal.ActionJournalConflictError("Action Assistant is unavailable")
        evidence = self._strategy.preflight(request)
        container_id, image = self._strategy.binding_identity(active)
        origins = excluded_origins if excluded_origins is not None else frozenset({stored_input_origin(request)})
        operation = action_operation(
            request,
            container_id,
            image,
            self._strategy.integration_generations(request),
            self._strategy.stored_input_generations(request, origins),
            action_files.commitments(evidence.file if isinstance(evidence, RpcPrivateInputs) else None),
        )
        return dataclasses.replace(operation, operation_id=self._strategy.operation_id(request)), evidence

    def _operation(
        self,
        request: object,
        excluded_origins: frozenset[str] | None = None,
    ) -> action_journal.Operation:
        return self._operation_with_evidence(request, excluded_origins)[0]

    def _prepared_stored_inputs_unchanged(self, request: object) -> bool:
        """Require every Stored Input present at prepare, whatever its origin, to keep its exact generation.

        Only a value absent at prepare and since sealed by a sibling of this batch may then change the strict
        fingerprint, and excluding this batch's origins must restore the prepared one.
        """
        present = self._prepared_stored_inputs[request.interrupt_id]
        current = dict(self._strategy.stored_input_generations(request, frozenset()))
        return all(current.get(stored_input_id) == generation for stored_input_id, generation in present.items())

    def prepare(self, requests: tuple[object, ...]) -> None:
        if self._batch is not None:
            raise action_journal.ActionJournalConflictError("Action batch is already prepared")
        self._origins = frozenset(stored_input_origin(request) for request in requests)
        operations = tuple(self._operation(request) for request in requests)
        self._prepared_stored_inputs = {
            request.interrupt_id: dict(self._strategy.stored_input_generations(request, frozenset()))
            for request in requests
        }
        if self._journal is None:
            self._journal = self._journal_source()
        self._batch = self._journal.prepare_batch(
            self._generation, self._thread_id, operations, archivable=self.archivable
        )
        self._operations = {operation.interrupt_id: operation for operation in operations}

    def invoke(self, request: object) -> object:
        if self._journal is None or self._batch is None:
            raise action_journal.ActionJournalConflictError("Action batch is not prepared")
        operation = self._operations.get(request.interrupt_id)
        if operation is None:
            raise action_journal.ActionJournalConflictError("Action operation is not prepared")
        current_operation, evidence = self._operation_with_evidence(request)
        if not self._prepared_stored_inputs_unchanged(request) or (
            current_operation != operation and self._operation(request, self._origins) != operation
        ):
            raise action_journal.ActionJournalConflictError("Action credential generation changed")
        with self._strategy.admit(request, evidence):
            return self._execute(request, operation, evidence)

    def _execute(self, request: object, operation: action_journal.Operation, evidence: object) -> object:
        decision = self._journal.begin(self._batch, operation)
        if not decision.execute:
            return decision.result
        self._executing_here.add(operation.interrupt_id)
        try:
            result = self._strategy.execute(request, evidence, decision.operation_id)
        except action_human.HumanRequestSuspensionError:
            self._journal.suspend(self._batch, operation)
            self._executing_here.discard(operation.interrupt_id)
            raise
        except Exception as exc:
            # A handled failure of a reviewed read-only Action had no business effect; every other failure, and any
            # failure of a mutating Action, stays uncertain in the journal (ADR-0092).
            if action_failure.failure_of(exc) is not None and self._strategy.effect(request) == "read_only":
                self._journal.fail_without_effect(self._batch, operation)
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
        self._release()

    def _release(self) -> None:
        self._batch = None
        self._operations = {}
        self._origins = frozenset()
        self._prepared_stored_inputs = {}
        self._executing_here.clear()
        if callable(self._journal_source):
            self._journal = None

    def terminate(self) -> bool:
        """End this undelivered batch when its turn fails, so the generation admits a fresh batch.

        An outcome made uncertain by this attempt is abandoned in-band. A settled batch, including one whose every
        Action completed before the Brain failed or Stop won, ends with its receipts kept for an exact replay only.
        An outcome left uncertain by an earlier process is never ended here.
        """
        if self._journal is None or self._batch is None:
            return False
        ended = self._abandon_uncertain() if self._executing_here else self._journal.end(self._batch)
        if ended:
            self._release()
        return ended

    def _abandon_uncertain(self) -> bool:
        return self._journal.abandon_uncertain(self._batch)


class HeldActionBatch(ActionBatch):
    """A Routine run's batch: an uncertain outcome is held for recovery, never abandoned in-band (ADR-0092).

    ``held`` is the fingerprint of the batch left uncertain, so the run is held and its recovery verifies it.
    """

    archivable = True

    held: str = ""

    def _abandon_uncertain(self) -> bool:
        self.held = self._batch.fingerprint
        return False


class RpcExchangeError(RuntimeError):
    """One stable failure kind translated into each Controller's public error shape.

    ``condition`` is the actual safe transport condition, such as ``exit-status:1`` or ``stderr-output``, recorded
    in place of any unverifiable raw child output (ADR-0092).
    """

    def __init__(self, kind: str, condition: str | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.condition = condition or kind


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
    # The reviewed English message catalog every request copy reference must name (ADR-0091).
    catalog: Mapping[str, Mapping[str, object]] | None = None
    # Capabilities Team injected into the workload, such as its egress token: protected like every injected value.
    capabilities: tuple[str, ...] = ()
    # A file-taking Action whose file content was withheld cannot have succeeded with it (ADR-0093).
    file_withheld: bool = False


_DEFAULT_RPC_RESULT_POLICY = RpcResultPolicy()


def _injected_values(integrations_by_id: Mapping[str, Mapping[str, object]], policy: RpcResultPolicy) -> dict[str, str]:
    """Every private value Team supplied to one invocation: tokens, secret responses, Stored Inputs, capabilities.

    Every envelope branch uses this one collection: the failure branch redacts these values, and every other branch
    refuses an echo of any of them outright.
    """
    secrets = protected_rpc_values(integrations_by_id)
    secrets.update({f"capability:{index}": value for index, value in enumerate(policy.capabilities)})
    if policy.protected_values is not None:
        secrets.update(policy.protected_values)
    if policy.stored_inputs_by_id is not None:
        secrets.update({f"stored-input:{key}": value for key, value in policy.stored_inputs_by_id.items()})
    return secrets


def _raise_failure(raw_result: object, secrets: tuple[str, ...]) -> NoReturn:
    """Admit a handled failure re-redacted with every injected value; a malformed frame is an invalid result."""
    try:
        failure = action_failure.admit(raw_result, secrets)
    except action_failure.FailureEnvelopeError as exc:
        raise RpcInvalidResultError from exc
    raise action_failure.ActionFailedError(failure)


def project_rpc_result(
    raw_result: object,
    integrations_by_id: Mapping[str, Mapping[str, object]],
    validate: Callable[[object], object],
    policy: RpcResultPolicy = _DEFAULT_RPC_RESULT_POLICY,
) -> object:
    """Reject private echoes, validate one tagged result, or raise one admitted suspension or handled failure.

    Only the failure branch is sanitized (ADR-0092): a result, request, or Stored Input rejection that echoes an
    injected value is still refused outright.
    """
    secrets = _injected_values(integrations_by_id, policy)
    if action_failure.is_failure(raw_result):
        _raise_failure(raw_result, tuple(secrets.values()))
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
                catalog=policy.catalog or {},
            )
        except action_human.HumanRequestError as exc:
            raise RpcInvalidResultError from exc
        if policy.authorization_requested and request.kind in action_human.AUTHORIZATION_KINDS:
            raise RpcInvalidResultError
        raise action_human.HumanRequestSuspensionError(request)
    if response_type != "result" or "result" not in raw_result or policy.file_withheld:
        raise RpcInvalidResultError
    try:
        result = validate(raw_result["result"])
        action_journal.require_durable_result(result)
    except (ValueError, action_journal.ActionJournalConflictError) as exc:
        raise RpcInvalidResultError from exc
    return result


def decode_rpc_response(raw: bytes) -> dict[str, object]:
    """Decode one direct Spec v1 Action result."""
    try:
        response = strict_json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RpcExchangeError("invalid-result", "frame-invalid") from exc
    if not isinstance(response, dict):
        raise RpcExchangeError("invalid-result", "frame-invalid")
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
    # An absolute monotonic deadline that already bounds this RPC, such as an admitted file delivery's (ADR-0093);
    # without one, the RPC's deadline starts when it is called.
    deadline: float | None = None


class _DispatchExpiredError(RuntimeError):
    """The RPC's deadline passed before its workload process was started."""


def _start_exec(container_id: str, argv: list[str], strategy: RpcExchangeStrategy, deadline: float) -> object:
    """Create and start the exec within the RPC's remaining budget; nothing starts once the deadline has passed.

    Setup runs on a worker so a slow Docker call cannot outlive the budget. When the wait expires, a stream that
    still arrives is closed, and the caller fail-stops the workload because the process may have started.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DispatchExpiredError("the Action deadline passed before dispatch")

    def setup() -> object:
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
        if time.monotonic() >= deadline:
            raise _DispatchExpiredError("the Action deadline passed before dispatch")
        return exec_id, strategy.api.exec_start(exec_id, socket=True)

    worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="action-rpc-setup")
    future = worker.submit(setup)
    worker.shutdown(wait=False)
    try:
        return future.result(timeout=remaining)
    except concurrent.futures.TimeoutError as exc:
        future.add_done_callback(lambda done: _close_late_stream(done, strategy))
        raise TimeoutError("the Action could not start within its deadline") from exc


def _close_late_stream(done: concurrent.futures.Future, strategy: RpcExchangeStrategy) -> None:
    # A setup that failed late has no stream to close; any error closing one is no longer anyone's to report.
    with suppress(Exception):
        strategy.close_stream(done.result()[1])


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
    # One absolute deadline bounds setup and exchange alike, so setup time is never added to the exchange.
    deadline = strategy.deadline if strategy.deadline is not None else time.monotonic() + strategy.timeout
    try:
        exec_id, stream = _start_exec(container_id, argv, strategy, deadline)
        if stream is None:
            raise OSError("Docker attach stream is unavailable")
        try:
            raw_socket = getattr(stream, "_sock", None)
            if raw_socket is None:
                raise OSError("Docker attach socket cannot half-close stdin")
            stdout, stderr = exchange_rpc_frames(raw_socket, encoded, deadline, strategy.maximum)
        finally:
            strategy.close_stream(stream)
    except _DispatchExpiredError as exc:
        # No workload process started, so there is nothing to stop.
        strategy.cancelled(exc)
        raise RpcExchangeError("timeout", "deadline-expired-before-dispatch") from exc
    except TimeoutError as exc:
        strategy.fail_stop()
        strategy.cancelled(exc)
        raise RpcExchangeError("timeout") from exc
    except (*transport_errors, OSError, ValueError, KeyError) as exc:
        strategy.fail_stop()
        strategy.cancelled(exc)
        condition = "frame-invalid" if isinstance(exc, ValueError) else "transport-failed"
        raise RpcExchangeError("failed", condition) from exc

    try:
        details = strategy.api.exec_inspect(exec_id)
    except transport_errors as exc:
        strategy.fail_stop()
        strategy.cancelled(exc)
        raise RpcExchangeError("ambiguous", "exit-unavailable") from exc
    exit_code = details.get("ExitCode")
    if not isinstance(exit_code, int):
        strategy.fail_stop()
        strategy.cancelled(None)
        raise RpcExchangeError("ambiguous", "exit-unavailable")
    if exit_code != 0 or stderr:
        if detect_unsupported_path and exit_code == 2 and not stdout and not stderr:
            raise RpcExchangeError("unsupported-path")
        # Only a clean exit with empty stderr may carry a handled failure frame; anything else is a transport fault,
        # whose workload is fail-stopped like every other ambiguous outcome before anything may verify it (ADR-0092).
        strategy.fail_stop()
        strategy.cancelled(None)
        raise RpcExchangeError("failed", f"exit-status:{exit_code}" if exit_code != 0 else "stderr-output")
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
    origins: frozenset[str],
    resolve: Callable[[str, object], action_stored_input.StoredInputValue],
) -> tuple[tuple[str, int], ...]:
    """Fingerprint reusable generations while preserving values just sealed by the same journal batch."""
    values = resolve_action_stored_inputs(actions, stored_inputs, action_id, resolve)
    generations: list[tuple[str, int]] = []
    for stored_input_id, value in values.items():
        if value.origin in origins:
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
    # The one selected file a file-taking Action's declared input names, as the turn bound it (ADR-0093).
    file: action_files.ActionFile | None = None


@dataclass(frozen=True, slots=True, repr=False)
class ActionInvocationEvidence:
    """Invoke-time private evidence, the memory-only replay transcript, and the journaled logical operation id."""

    private_inputs: RpcPrivateInputs
    transcript: action_human.ActionTranscript
    origin: str
    operation_id: str


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedInvocationEvidence:
    """Private values selected for an invocation, including direct calls without replay."""

    integrations: Mapping[str, Mapping[str, object]]
    stored_inputs: Mapping[str, str]
    transcript: action_human.ActionTranscript
    origin: str | None
    operation_id: str
    file: action_files.ActionFile | None = None


def resolve_invocation_evidence(
    evidence: ActionInvocationEvidence | None,
    resolve_integrations: Callable[[], Mapping[str, Mapping[str, object]]],
    resolve_stored_inputs: Callable[[], Mapping[str, action_stored_input.StoredInputValue]],
) -> ResolvedInvocationEvidence:
    """Use frozen chat evidence or resolve exact private values for a direct invocation.

    A direct invocation is not journaled and never replayed, so it is one fresh logical operation.
    """
    if evidence is not None:
        return ResolvedInvocationEvidence(
            evidence.private_inputs.integrations,
            evidence.private_inputs.stored_inputs,
            evidence.transcript,
            evidence.origin,
            evidence.operation_id,
            evidence.private_inputs.file,
        )
    resolved = resolve_stored_inputs()
    return ResolvedInvocationEvidence(
        resolve_integrations(),
        {stored_input_id: value.value for stored_input_id, value in resolved.items()},
        action_human.ActionTranscript(""),
        None,
        action_journal.new_operation_id(),
    )


def require_rpc_envelope(
    active: object,
    request: object,
    resolve_integrations: Callable[[object, str], Mapping[str, Mapping[str, object]]],
    resolve_stored_inputs: Callable[[object, str], Mapping[str, action_stored_input.StoredInputValue]],
    file: action_files.ActionFile | None = None,
) -> RpcPrivateInputs:
    """Resolve and size-check the exact Spec v1 invocation, with any file's content withheld, before journaling."""
    integrations = resolve_integrations(active, request.action)
    resolved_stored_inputs = resolve_stored_inputs(active, request.action)
    stored_inputs = {stored_input_id: resolved.value for stored_input_id, resolved in resolved_stored_inputs.items()}
    encode_rpc_invocation(
        request.input,
        integration_access_tokens(integrations),
        stored_inputs,
        OPERATION_ID_PLACEHOLDER,
        files=action_files.invocation_files(file, False, lambda _file_id: ({}, b"")),
    )
    return RpcPrivateInputs(integrations, stored_inputs, file)


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


class _FrameReader:
    """Parse Docker's multiplexed exec frames incrementally within one cumulative output bound."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._pending = bytearray()
        self._stdout = bytearray()
        self._stderr = bytearray()

    def feed(self, data: bytes) -> None:
        self._pending.extend(data)
        while len(self._pending) >= _FRAME_HEADER_BYTES:
            stream_id, length = struct.unpack(">BxxxL", self._pending[:_FRAME_HEADER_BYTES])
            if stream_id not in {1, 2}:
                raise ValueError("invalid Assistant RPC stream")
            if length > self._maximum + 1:
                raise ValueError("oversized Assistant RPC frame")
            end = _FRAME_HEADER_BYTES + length
            if len(self._pending) < end:
                return
            (self._stdout if stream_id == 1 else self._stderr).extend(self._pending[_FRAME_HEADER_BYTES:end])
            del self._pending[:end]
            if len(self._stdout) + len(self._stderr) > self._maximum:
                raise ValueError("oversized Assistant RPC response")

    def finish(self) -> tuple[bytes, bytes]:
        if self._pending:
            raise ValueError("truncated Assistant RPC frame")
        return bytes(self._stdout), bytes(self._stderr)


_FRAME_HEADER_BYTES = 8
_CHUNK_BYTES = 64 * 1024


def exchange_rpc_frames(raw_socket: socket.socket, data: bytes, deadline: float, maximum: int) -> tuple[bytes, bytes]:
    """Write stdin while draining output, then half-close; return the bounded stdout and stderr at end of stream.

    Reading and writing interleave on a non-blocking socket within one deadline, so a workload that answers before it
    reads all of a large invocation never stalls on a full buffer (ADR-0093).
    """
    reader = _FrameReader(maximum)
    view = memoryview(data)
    sent = 0
    previous = raw_socket.gettimeout()
    raw_socket.setblocking(False)
    try:
        while True:
            remaining = deadline - time.monotonic()
            writing = sent < len(view)
            if remaining <= 0:
                raise TimeoutError
            readable, writable, _ = select.select([raw_socket], [raw_socket] if writing else [], [], remaining)
            if not readable and not writable:
                raise TimeoutError
            if writable:
                try:
                    sent += raw_socket.send(view[sent : sent + _CHUNK_BYTES])
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    # The workload stopped reading its input; its output and exit status still decide the outcome.
                    sent = len(view)
                if sent == len(view):
                    with suppress(OSError):
                        raw_socket.shutdown(socket.SHUT_WR)
            if readable:
                try:
                    chunk = raw_socket.recv(_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    return reader.finish()
                reader.feed(chunk)
    finally:
        raw_socket.settimeout(previous)


def read_rpc_frames(raw_socket: socket.socket, deadline: float, maximum: int) -> tuple[bytes, bytes]:
    """Read Docker's multiplexed exec frames to end of stream with the same bounded parser."""
    return exchange_rpc_frames(raw_socket, b"", deadline, maximum)


def close_exec_stream(stream: object) -> None:
    """Close docker-py's owning HTTP response before its raw socket."""
    response = getattr(stream, "_response", None)
    if response is not None:
        response.close()
    else:
        stream.close()
