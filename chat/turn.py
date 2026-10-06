"""Shared Team-owned chat turn drive and suspension dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import NoReturn

from action import human as action_human
from action import journal as action_journal
from assistant import language as assistant_language
from chat import contract as assistant_chat
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from inference import client as brain_runtime_client
from inference import usage as brain_usage
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from integrations import store as integration_store

CHAT_PAUSED_STATUSES = frozenset({"human-required", "integrations-required"})


@dataclass(slots=True)
class SegmentRequirements:
    """Mutable suspension gates populated while one shared segment is driven."""

    integrations: tuple[object, ...] = ()
    human: tuple[object, ...] = ()
    # The fingerprint of the Action batch a human request paused, so ending that turn removes exactly this batch.
    paused_batch: str | None = None

    def groups(self) -> tuple[tuple[object, ...], ...]:
        return self.integrations, self.human


@dataclass(frozen=True, slots=True)
class PreparedSegment:
    """Controller-specific resources consumed by the shared segment state machine."""

    team_name: str
    identity: tuple[object, ...]
    context: object
    files: list[dict[str, object]]
    durable_batch: object


@dataclass(frozen=True, slots=True)
class SegmentResult:
    """Named result of one Controller segment, including its single suspension gate."""

    team_name: str
    identity: tuple[object, ...]
    outcome: chat_orchestrator.ChatOutcome | chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension
    integrations: tuple[object, ...]
    human: tuple[object, ...] = ()
    # The exact contract digest of each Assistant the Brain saw in this segment, for binding a Routine proposal.
    contracts: tuple[tuple[str, str], ...] = ()
    # The interface language the turn's start pinned; a suspension keeps it for the rest of the turn (ADR-0091).
    locale: str | None = None
    # The fingerprint of the Action batch a human request paused; None for any other outcome.
    paused_batch: str | None = None
    # The model the segment's Brain ran on, which a suspension keeps for the rest of the turn.
    model: str | None = None

    def requirement_groups(self) -> tuple[tuple[object, ...], ...]:
        return self.integrations, self.human


@dataclass(frozen=True, slots=True)
class SegmentStrategy:
    """Hosted/local adapters for state and errors that intentionally differ."""

    runtime: object
    prepare: Callable[[], PreparedSegment]
    validate_action: Callable
    pause_for_private_inputs: Callable[[tuple[object, ...], SegmentRequirements], bool]
    cancelled: Callable[[], bool]
    validate_context: Callable[[], None]
    raise_problem: Callable[[str, BaseException | None], None]
    # Builds the requirement with its copy rendered in the given concrete interface language (ADR-0091).
    human_requirement: Callable[[object, action_human.HumanRequest, str], object] = lambda _action, _request, _locale: (
        _ for _ in ()
    ).throw(chat_orchestrator.ChatOrchestrationError("Action human requests are unavailable"))
    finalize: Callable[[], None] = lambda: None
    progress: chat_progress.Reporter = field(default_factory=chat_progress.Reporter)
    # A compiled Routine run's own round limit and its choice to keep no invoked Actions (ADR-0092, 2026-10-05, scale).
    max_rounds: int = chat_orchestrator.MAX_ACTION_ROUNDS
    record_invoked: bool = True


@dataclass(frozen=True, slots=True)
class IntegrationResumeStrategy:
    """Controller adapters for admitting one integration-gated continuation."""

    store: object
    team_id: str
    challenge_id: object
    pending_valid: Callable[[object], bool]
    pending_identity: Callable[[object], tuple[object, ...]]
    inspect: Callable[[object], IntegrationResumeContext]
    integration_store: object
    challenge_response: Callable[[object], object]
    expired_error: Callable[[], BaseException]
    context_error: Callable[[], BaseException]
    contract_error: Callable[[], BaseException]
    end_drifted: Callable[[object], None]


@dataclass(frozen=True, slots=True)
class IntegrationResumeContext:
    identity: tuple[object, ...]
    bindings: object
    requests: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class IntegrationResumeAdmission:
    pending: object | None
    response: object | None


_DRIVE_ERRORS = (
    action_journal.ActionJournalError,
    chat_orchestrator.ChatStoppedError,
    chat_orchestrator.ChatOrchestrationError,
    brain_runtime_client.BrainRuntimeError,
)


def admit_integration_resume(strategy: IntegrationResumeStrategy) -> IntegrationResumeAdmission:
    """Make integration challenge, identity, requirement and one-use decisions once for both twins."""
    try:
        challenge = strategy.store.get(strategy.team_id, strategy.challenge_id)
    except integration_challenges.IntegrationChallengeNotFoundError as exc:
        raise strategy.expired_error() from exc
    pending = challenge.payload
    if not strategy.pending_valid(pending):
        raise strategy.context_error()
    # The challenge itself is inspected, so a Controller can end exactly the paused turn whose context drifted.
    context = strategy.inspect(challenge)
    if context.identity != strategy.pending_identity(pending):
        strategy.end_drifted(challenge)
        raise strategy.context_error()
    try:
        missing = integration_flow.requirements_for_batch(
            strategy.team_id,
            context.bindings,
            context.requests,
            strategy.integration_store,
        )
    except (integration_flow.IntegrationFlowError, integration_store.OAuthIntegrationStoreError) as exc:
        raise strategy.contract_error() from exc
    if missing:
        return IntegrationResumeAdmission(None, strategy.challenge_response(challenge))
    try:
        claimed = strategy.store.claim(strategy.team_id, challenge.id)
    except integration_challenges.IntegrationChallengeNotFoundError as exc:
        raise strategy.expired_error() from exc
    if claimed is not challenge:
        raise strategy.expired_error()
    return IntegrationResumeAdmission(pending, None)


def run_segment(
    strategy: SegmentStrategy,
    *,
    message: str | None,
    continuation: chat_orchestrator.ChatContinuation | None,
    expected_identity: tuple[object, ...] | None,
    conversation: tuple[brain_runtime_client.RuntimeConversationEntry, ...] = (),
) -> tuple[
    str,
    tuple[object, ...],
    chat_orchestrator.ChatOutcome | chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension,
    SegmentRequirements,
]:
    """Apply the same continuation, identity and suspension decisions on both Controllers."""
    if (message is None) == (continuation is None) or (conversation and message is None):
        strategy.raise_problem("invalid-continuation", None)
    with strategy.progress.span("team-context"):
        segment = strategy.prepare()
    if expected_identity is not None and segment.identity != expected_identity:
        strategy.raise_problem("context-changed", None)
    requirements = SegmentRequirements()
    try:
        outcome = drive(
            strategy=strategy,
            segment=segment,
            message=message,
            continuation=continuation,
            requirements=requirements,
            conversation=conversation,
        )
    except Exception as exc:
        try:
            segment.durable_batch.terminate()
        except action_journal.ActionJournalError as abandonment_error:
            strategy.raise_problem("drive-error", abandonment_error)
            raise AssertionError("chat error adapter returned") from abandonment_error
        if isinstance(exc, _DRIVE_ERRORS):
            strategy.raise_problem("drive-error", exc)
            raise AssertionError("chat error adapter returned") from exc
        raise
    strategy.finalize()
    groups = requirements.groups()
    if (
        isinstance(outcome, chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension)
        and suspension_gate_count(*groups) != 1
    ) or (isinstance(outcome, chat_orchestrator.ChatHumanSuspension) and requirements.paused_batch is None):
        strategy.raise_problem("invalid-suspension", None)
    return segment.team_name, segment.identity, outcome, requirements


def drive(
    *,
    strategy: SegmentStrategy,
    segment: PreparedSegment,
    message: str | None = None,
    continuation: chat_orchestrator.ChatContinuation | None = None,
    requirements: SegmentRequirements,
    conversation: tuple[brain_runtime_client.RuntimeConversationEntry, ...] = (),
) -> chat_orchestrator.ChatOutcome | chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension:
    """Run or resume one turn with the same durable Action hooks on both Controllers."""

    def pause_before_batch(requests: tuple[object, ...]) -> bool:
        return strategy.pause_for_private_inputs(requests, requirements)

    orchestration = chat_orchestrator.ChatStrategy(
        validate_action=strategy.validate_action,
        invoke_action=segment.durable_batch.invoke,
        prepare_batch=segment.durable_batch.prepare,
        batch_delivered=segment.durable_batch.delivered,
        pause_before_batch=pause_before_batch,
        cancelled=strategy.cancelled,
        validate_context=strategy.validate_context,
        progress=strategy.progress,
        max_rounds=strategy.max_rounds,
        record_invoked=strategy.record_invoked,
    )
    if continuation is None:
        outcome = chat_orchestrator.run_until_pause(
            strategy.runtime,
            segment.context,
            assistant_chat.build_prompt(message, segment.files),
            orchestration,
            conversation,
        )
    else:
        outcome = chat_orchestrator.continue_after_pause(
            strategy.runtime,
            segment.context,
            continuation,
            orchestration,
        )
    if isinstance(outcome, chat_orchestrator.ChatHumanSuspension):
        requirements.paused_batch = segment.durable_batch.fingerprint
        requirements.human = (_with_purpose(strategy, segment, outcome),)
    return outcome


def _with_purpose(
    strategy: SegmentStrategy,
    segment: PreparedSegment,
    suspension: chat_orchestrator.ChatHumanSuspension,
) -> object:
    """Render a new human requirement in the turn's language and attach the Brain's task-bound purpose (ADR-0090).

    A turn without a concrete interface language renders the English catalog and asks for no purpose: a purpose is
    shown only in a challenge of its own concrete locale (ADR-0091). The purpose is optional: any Brain failure leaves
    it absent. Stop and a changed Team context still end the turn, so they are checked again after the call and before
    the challenge exists.
    """
    locale = segment.context.locale
    requirement = strategy.human_requirement(
        suspension.action, suspension.request, locale or assistant_language.ENGLISH
    )
    if locale is None:
        return requirement
    purpose = strategy.runtime.purpose(
        segment.context,
        suspension.action,
        requirement.assistant_name,
        requirement.action_summary,
    )
    if strategy.cancelled():
        raise chat_orchestrator.ChatStoppedError("chat turn stopped")
    strategy.validate_context()
    return replace(requirement, purpose=purpose, purpose_locale=None if purpose is None else locale)


def suspension_gate_count(*requirements: tuple[object, ...]) -> int:
    return sum(bool(group) for group in requirements)


def _raise_unreachable_suspension() -> NoReturn:
    raise AssertionError("unreachable")


def commit_suspension(
    continuation: object,
    pending_continuation: object,
    commit: Callable[[], bool],
    cancel: Callable[[], None],
    stopped_error: Callable[[], BaseException],
    cleanup: Callable[[], None] = lambda: None,
) -> None:
    """Commit one matching suspension or roll back every persisted challenge artifact."""
    if continuation == pending_continuation and commit():
        return
    cancel()
    cleanup()
    raise stopped_error()


def dispatch(
    outcome: chat_orchestrator.ChatOutcome | chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension,
    requirements: tuple[tuple[object, ...], ...],
    pending: Callable[[object], object],
    pause: tuple[Callable[[object, tuple[object, ...], object], object], ...],
    complete: Callable[[chat_orchestrator.ChatOutcome], object],
) -> object:
    """Send exactly one suspension kind to its handler, or finish a terminal turn."""
    if not isinstance(outcome, chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension):
        return complete(outcome)
    if len(requirements) != len(pause) or suspension_gate_count(*requirements) != 1:
        raise ValueError("invalid chat suspension")
    state = pending(outcome)
    for group, handler in zip(requirements, pause, strict=True):
        if group:
            return handler(outcome, group, state)
    _raise_unreachable_suspension()


def terminal_body(
    team_id: str,
    team_name: str,
    outcome: chat_orchestrator.ChatOutcome,
    usage: brain_usage.TurnUsage | None,
) -> dict[str, object]:
    """A completed turn's reply body, with the whole turn's usage when it was metered."""
    body: dict[str, object] = {
        "team_id": team_id,
        "team_name": team_name,
        "reply": outcome.reply,
        "clarification": outcome.clarification,
    }
    wire = None if usage is None else usage.joined().wire()
    if wire is not None:
        body["usage"] = wire
    return with_restricted_actions(body, outcome)


def with_restricted_actions(body: dict[str, object], outcome: chat_orchestrator.ChatOutcome) -> dict[str, object]:
    """A completed terminal body, naming the Actions withheld for attachment content when there were any."""
    if outcome.restricted_actions is not None:
        body["restricted_actions"] = outcome.restricted_actions
    return body
