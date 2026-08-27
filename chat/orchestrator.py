"""Deterministic Team-owned loop between LangGraph suspensions and Assistant Actions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from action import human as action_human
from chat import progress as chat_progress
from inference import client as brain_runtime_client

MAX_ACTION_ROUNDS = 8


class ChatOrchestrationError(RuntimeError):
    """The Brain runtime violated the turn contract or could not finish safely."""


class ChatStoppedError(ChatOrchestrationError):
    """The Controller cancelled the active turn between two bounded operations."""


@dataclass(frozen=True, slots=True)
class InvokedAction:
    assistant_id: str
    action: str


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    reply: str
    actions: tuple[InvokedAction, ...]


@dataclass(frozen=True, slots=True)
class ChatContinuation:
    """In-memory, secret-free state needed to continue one LangGraph suspension."""

    turn: brain_runtime_client.RuntimeTurn
    seen_interrupts: tuple[str, ...]
    invoked: tuple[InvokedAction, ...]
    round_index: int


@dataclass(frozen=True, slots=True)
class ChatSuspension:
    continuation: ChatContinuation
    requests: tuple[brain_runtime_client.ActionRequest, ...]


@dataclass(frozen=True, slots=True)
class ChatHumanSuspension:
    """A validated Action request paused without returning anything to the Brain."""

    continuation: ChatContinuation
    action: brain_runtime_client.ActionRequest
    request: action_human.HumanRequest
    completed_interrupts: tuple[str, ...] = ()


def retain_suspension_transcripts(
    transcripts: tuple[action_human.ActionTranscript, ...],
    suspension: ChatSuspension | ChatHumanSuspension,
) -> tuple[action_human.ActionTranscript, ...]:
    """Drop transcripts for delivered rounds and completed Actions in the current batch."""
    completed = suspension.continuation.seen_interrupts
    if isinstance(suspension, ChatHumanSuspension):
        completed = (*completed, *suspension.completed_interrupts)
    return action_human.retain_unfinished_transcripts(transcripts, completed)


ActionInvoker = Callable[[brain_runtime_client.ActionRequest], object]
ActionValidator = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]
BatchHook = Callable[[tuple[brain_runtime_client.ActionRequest, ...]], None]
BatchPause = Callable[[tuple[brain_runtime_client.ActionRequest, ...]], bool]
CancellationCheck = Callable[[], bool]
ContextCheck = Callable[[], None]


@dataclass(frozen=True, slots=True)
class ChatStrategy:
    validate_action: ActionValidator
    invoke_action: ActionInvoker
    prepare_batch: BatchHook = lambda _batch: None
    batch_delivered: BatchHook = lambda _batch: None
    pause_before_batch: BatchPause = lambda _batch: False
    cancelled: CancellationCheck = lambda: False
    validate_context: ContextCheck = lambda: None
    progress: chat_progress.Reporter = field(default_factory=chat_progress.Reporter)


def _validate_batch(
    requests: tuple[brain_runtime_client.ActionRequest, ...],
    declared: Mapping[tuple[str, str], brain_runtime_client.RuntimeAction],
    validate_action: ActionValidator,
) -> tuple[brain_runtime_client.ActionRequest, ...]:
    """Validate a complete suspension before allowing its first side effect."""
    if not requests:
        raise ChatOrchestrationError("Brain suspended without an Action request")

    seen_interrupts: set[str] = set()
    contracts: list[tuple[brain_runtime_client.ActionRequest, brain_runtime_client.RuntimeAction]] = []
    for request in requests:
        action = declared.get((request.assistant_id, request.action))
        if action is None:
            raise ChatOrchestrationError("Brain requested an undeclared Action contract")
        if request.interrupt_id in seen_interrupts:
            raise ChatOrchestrationError("Brain repeated an Action interrupt id")
        seen_interrupts.add(request.interrupt_id)
        contracts.append((request, action))

    validated: list[brain_runtime_client.ActionRequest] = []
    for request, action in contracts:
        safe_input = validate_action(request.assistant_id, action.id, request.input)
        if not isinstance(safe_input, Mapping):
            raise ChatOrchestrationError("Action validator returned an invalid input contract")
        validated.append(
            brain_runtime_client.ActionRequest(
                interrupt_id=request.interrupt_id,
                assistant_id=request.assistant_id,
                action=action.id,
                input=dict(safe_input),
            )
        )
    return tuple(validated)


def _drive(
    runtime: brain_runtime_client.BrainRuntimeClient,
    context: brain_runtime_client.RuntimeContext,
    continuation: ChatContinuation,
    strategy: ChatStrategy,
) -> ChatOutcome | ChatSuspension | ChatHumanSuspension:
    turn = continuation.turn
    invoked = list(continuation.invoked)
    seen_interrupts = set(continuation.seen_interrupts)
    declared = {(assistant.id, action.id): action for assistant in context.assistants for action in assistant.actions}

    for _round in range(continuation.round_index, MAX_ACTION_ROUNDS + 1):
        if strategy.cancelled():
            raise ChatStoppedError("chat turn stopped")
        if turn.status == "completed":
            with strategy.progress.span("team-context"):
                strategy.validate_context()
            return ChatOutcome(reply=turn.reply, actions=tuple(invoked))
        if _round == MAX_ACTION_ROUNDS:
            raise ChatOrchestrationError("Brain exceeded the Action round limit")

        with strategy.progress.span("action-preparation"):
            strategy.validate_context()
            batch = _validate_batch(turn.actions, declared, strategy.validate_action)
            batch_interrupts = {request.interrupt_id for request in batch}
            if not seen_interrupts.isdisjoint(batch_interrupts):
                raise ChatOrchestrationError("Brain repeated an Action interrupt across rounds")
            if strategy.pause_before_batch(batch):
                return ChatSuspension(
                    continuation=ChatContinuation(
                        turn=turn,
                        seen_interrupts=tuple(sorted(seen_interrupts)),
                        invoked=tuple(invoked),
                        round_index=_round,
                    ),
                    requests=batch,
                )
            strategy.prepare_batch(batch)
        results: dict[str, object] = {}
        batch_invoked: list[InvokedAction] = []
        checkpoint = ChatContinuation(
            turn=turn,
            seen_interrupts=tuple(sorted(seen_interrupts)),
            invoked=tuple(invoked),
            round_index=_round,
        )
        for index, request in enumerate(batch, start=1):
            if strategy.cancelled():
                raise ChatStoppedError("chat turn stopped")
            strategy.validate_context()
            with strategy.progress.span(
                "action",
                index=index,
                total=len(batch),
                assistant_id=request.assistant_id,
                action=request.action,
            ):
                try:
                    result = strategy.invoke_action(request)
                except action_human.HumanRequestSuspensionError as exc:
                    return ChatHumanSuspension(checkpoint, request, exc.request, tuple(results))
            results[request.interrupt_id] = result
            batch_invoked.append(InvokedAction(assistant_id=request.assistant_id, action=request.action))

        strategy.validate_context()
        seen_interrupts.update(batch_interrupts)
        with strategy.progress.span("model"):
            resumed = runtime.resume(context, results)
        if resumed.status == "action-required" and not seen_interrupts.isdisjoint(
            request.interrupt_id for request in resumed.actions
        ):
            raise ChatOrchestrationError("Brain repeated an Action interrupt across rounds")
        with strategy.progress.span("action-delivery"):
            strategy.batch_delivered(batch)
        invoked.extend(batch_invoked)
        turn = resumed

    raise ChatOrchestrationError("Brain did not complete the chat turn")


def run_until_pause(
    runtime: brain_runtime_client.BrainRuntimeClient,
    context: brain_runtime_client.RuntimeContext,
    message: str,
    strategy: ChatStrategy,
) -> ChatOutcome | ChatSuspension | ChatHumanSuspension:
    """Start a turn and optionally pause before an all-or-nothing Action batch."""
    if strategy.cancelled():
        raise ChatStoppedError("chat turn stopped")
    strategy.validate_context()
    with strategy.progress.span("model"):
        turn = runtime.start(context, message)
    return _drive(
        runtime,
        context,
        ChatContinuation(turn=turn, seen_interrupts=(), invoked=(), round_index=0),
        strategy,
    )


def continue_after_pause(
    runtime: brain_runtime_client.BrainRuntimeClient,
    context: brain_runtime_client.RuntimeContext,
    continuation: ChatContinuation,
    strategy: ChatStrategy,
) -> ChatOutcome | ChatSuspension | ChatHumanSuspension:
    """Continue an admitted in-memory suspension without re-running the user turn."""
    return _drive(
        runtime,
        context,
        continuation,
        strategy,
    )


def run(
    runtime: brain_runtime_client.BrainRuntimeClient,
    context: brain_runtime_client.RuntimeContext,
    message: str,
    strategy: ChatStrategy,
) -> ChatOutcome:
    """Run a bounded turn; every model-requested Action returns through Controller validation."""
    outcome = run_until_pause(
        runtime,
        context,
        message,
        strategy,
    )
    if isinstance(outcome, ChatSuspension | ChatHumanSuspension):
        raise ChatOrchestrationError("chat turn paused without a Controller continuation")
    return outcome
