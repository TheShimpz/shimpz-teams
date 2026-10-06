"""Local chat segment orchestration operations."""

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus

from action import challenges as action_challenges
from action import execution as action_execution
from action import files as action_files
from action import human as action_human
from action import journal as action_journal
from chat import attachments as chat_attachments
from chat import knowledge as chat_knowledge
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import inference as local_inference
from local.chat import attachments as local_attachments
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import required_active_assistant as _required_active_assistant
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import recorder as routine_recorder
from local.validation import brain_thread_id as _brain_thread_id
from local.validation import routine_thread_id as _routine_thread_id
from protocol.http.v1 import routine as http_routine
from routine import pin as routine_pin
from routine import record as routine_record


@dataclass(frozen=True, slots=True)
class RoutineSegment:
    """One compiled Routine run (ADR-0092): its generation, its plan's runtime, and the held batch it prepares.

    The runtime answers the run's turns from the plan in place of the Brain and seals each dispatch before its RPC
    through ``dispatching``. The generation and journal thread label both derive from the run id in the Team's current
    network, never supplied.
    """

    run_id: str
    generation: str
    runtime: object
    batches: list[action_execution.HeldActionBatch] = field(default_factory=list)
    # A run's own segment holds an uncertain batch for recovery; a read-only verification ends its batch in-band.
    held: bool = True


@dataclass(frozen=True, slots=True)
class SegmentRequest:
    team_id: str
    file_ids: list[str]
    assistant_ids: tuple[str, ...]
    provider: str
    api_key: str
    token: str
    message: str | None = None
    # Committed presentation history before a new turn; never set for a continuation.
    conversation: tuple[brain_runtime_client.RuntimeConversationEntry, ...] = ()
    # The interface language a new turn is written in; a continuation carries the one its start pinned, so a later
    # request of the same turn renders in it (ADR-0090, ADR-0091).
    locale: str | None = None
    continuation: chat_orchestrator.ChatContinuation | None = None
    expected_identity: tuple[object, ...] | None = None
    transcripts: tuple[action_human.ActionTranscript, ...] = ()
    requests_used: int = 0
    progress: chat_progress.Reporter = field(default_factory=chat_progress.Reporter)
    # A compiled Routine run (ADR-0092) runs in its own journal generation in the Team's current network,
    # and holds an uncertain batch for a human instead of abandoning it.
    routine: RoutineSegment | None = None
    # The recording of a new logical chat turn that may define a Routine, by id (ADR-0101 section 4.1): only its start
    # offers the Brain the Team's Routines and its Routine tool, and every segment of it keeps its Action calls.
    recording: str | None = None


def _protector(self, request: SegmentRequest) -> Callable[[tuple[str, ...]], object] | None:
    """Where an attempt's further injected values are protected before its RPC: its run's, or its recording's."""
    if request.routine is not None:
        return request.routine.runtime.protect
    if request.recording is not None:
        return lambda values: self.routine_recordings.protect(request.team_id, request.recording, values)
    return None


def _observed(invoke: Callable[[], object], runtime, action_request, evidence) -> object:
    """One Action call of a compiled run: its failure is kept, and its own time ends at its return (ADR-0092, scale).

    The time ends before Team journals, seals, or projects anything of the call.
    """
    try:
        return invoke()
    except ApiProblem as exc:
        runtime.failed(action_request, evidence, exc)
        raise
    finally:
        # Every exit ends the Action's own time, a Stop's included; failed() already ended it first.
        runtime.returned()


def runtime_assistant(active: _ActiveAssistant, genesis: str) -> brain_runtime_client.RuntimeAssistant:
    """The Assistant exactly as a Brain turn sees it; its contract digest pins skills and Routines."""
    return brain_runtime_client.RuntimeAssistant(
        id=active.spec.assistant_id,
        genesis=genesis,
        actions=tuple(
            chat_attachments.runtime_action(action_id, action)
            for action_id, action in sorted(active.spec.actions.items())
        ),
    )


def routine_scope(active: _ActiveAssistant, genesis: str) -> str:
    """The pin a Routine holds for one Assistant: its Brain-visible contract and every Action's complete pin."""
    return routine_pin.assistant_pin(
        active.spec, brain_runtime_client.contract_digest(runtime_assistant(active, genesis))
    )


def _human_requirement(
    self,
    bindings: dict[str, _ActiveAssistant],
    action_request: brain_runtime_client.ActionRequest,
    human_request: action_human.HumanRequest,
    locale: str,
    selected: dict[str, action_files.ActionFile] | None = None,
) -> action_challenges.HumanRequirement:
    """The paused request of one active Assistant, its copy rendered in the turn's language (ADR-0091).

    An authorization of a file-taking Action also discloses the selected file its approval delivers (ADR-0093).
    """
    active = _required_active_assistant(bindings, action_request.assistant_id)
    action = active.spec.actions.get(action_request.action)
    if action is None:
        raise chat_orchestrator.ChatOrchestrationError("Action human request contract changed")
    try:
        copy = action_challenges.render_copy(human_request, self._assistant_language(active), locale)
        file = action_files.disclosure(action.input_files, action_request.input, selected or {}, human_request.kind)
    except action_challenges.HumanChallengeError as exc:
        raise chat_orchestrator.ChatOrchestrationError("Action human request copy is unavailable") from exc
    except action_files.FileDeliveryError as exc:
        raise chat_orchestrator.ChatOrchestrationError("Action file is unavailable") from exc
    return action_challenges.HumanRequirement(
        active.spec.assistant_id,
        active.spec.name,
        action_request.action,
        action.summary,
        action_request.interrupt_id,
        human_request,
        active.spec.version,
        copy,
        help_url=action_challenges.declared_help_url(human_request, active.spec.stored_inputs),
        file=file,
    )


def _knowledge(self, team_id: str) -> tuple[object, object]:
    try:
        return self.inference_store.load_knowledge(team_id)
    except inference_config.InferenceConfigError as exc:
        local_inference._raise_inference_problem(exc)


def _routine_mutable(request: SegmentRequest) -> bool:
    """Whether this segment starts a turn that may define a Routine: a new chat turn being recorded, without files."""
    return request.routine is None and request.recording is not None and request.continuation is None


@dataclass(frozen=True, slots=True)
class _TurnScope:
    thread_id: str
    team_name: str
    assistants: tuple[_ActiveAssistant, ...]
    genesis_by_id: dict[str, str]
    files: list[dict[str, object]]
    config: object


def _turn_context(self, request: SegmentRequest, scope: _TurnScope) -> brain_runtime_client.RuntimeContext:
    """What the Brain sees in this segment: Assistants, knowledge, Routines, and the message's prepared files."""
    routine = request.routine
    # A compiled Routine run asks no model, so it reads no knowledge; a chat reads it at every segment, and the
    # Brain keeps what a logical turn started with across resumes.
    memories, skills = ((), ()) if routine is not None else _knowledge(self, request.team_id)
    runtime_assistants = tuple(
        runtime_assistant(active, scope.genesis_by_id[active.spec.assistant_id]) for active in scope.assistants
    )
    config = scope.config
    mutable = _routine_mutable(request)
    return brain_runtime_client.RuntimeContext(
        thread_id=scope.thread_id,
        team_name=scope.team_name,
        assistants=runtime_assistants,
        provider=config.provider,
        model=config.model,
        api_key=request.api_key,
        effort=config.effort,
        memories=None if routine is not None else tuple(memories),
        skills=None if routine is not None else chat_knowledge.turn_skills(skills, runtime_assistants),
        routines=_listed(self, request) if mutable else None,
        routine_capacity=self._routine_capacity(request.team_id) if mutable else None,
        routine_question=_pending_question(self, request) if mutable else None,
        routine_mode=mutable and routine_recorder.routine_mode(_span(self, request)),
        routine_rerun=routine_recorder.rerun_work(_span(self, request)) if mutable else None,
        knowledge_writable=routine is None,
        locale=request.locale,
        attachments=local_attachments.turn_attachments(self, request.team_id, request.token, scope.files),
    )


def _listed(self, request: SegmentRequest) -> tuple[dict[str, object], ...]:
    """The Team's Routines for a recording turn's Brain, kept with the recording as the only ones it may replace."""
    routines = self._chat_routines(request.team_id)
    revisions = ((item["routine_id"], item["revision"]) for item in routines)
    self.routine_recordings.listed(request.team_id, request.recording, revisions)
    return routines


def _span(self, request: SegmentRequest) -> routine_recorder.Span | None:
    return self.routine_recordings.live(request.team_id, request.recording)


def _pending_question(self, request: SegmentRequest) -> dict[str, object] | None:
    """The question Team asked in the recording this send continues, which the Brain sees beside the person's words."""
    span = _span(self, request)
    return None if span is None or span.asked is None else span.asked.wire


def _runtime(self, request: SegmentRequest) -> object:
    """Who answers this segment: a Routine run's runtime, Team itself, or the Brain.

    Team answers a send itself only when it is Admin's composed answer that binds the pending Routine question.
    """
    if request.routine is not None:
        return request.routine.runtime
    if request.recording is not None and request.continuation is None:
        intent = routine_recorder.answered(self.routine_recordings.live(request.team_id, request.recording))
        if intent is not None:
            return routine_recorder.AnsweredRuntime(http_routine.answer_reply(request.locale), intent)
    return self.brain_runtime


@contextmanager
def _admitted_delivery(
    self,
    request: SegmentRequest,
    bindings: dict[str, _ActiveAssistant],
    action_request: brain_runtime_client.ActionRequest,
    private_inputs: object,
) -> Iterator[None]:
    """Admit a file delivery to the one file-RPC slot before its attempt is journaled (ADR-0093)."""
    action = _required_active_assistant(bindings, action_request.assistant_id).spec.actions[action_request.action]
    try:
        with action_files.admitted(
            getattr(private_inputs, "file", None),
            action.human_requests,
            action_human.transcript_for(request.transcripts, action_request.interrupt_id),
            lambda: self._chat_cancelled(request.token),
        ):
            yield
    except action_files.FileRpcCancelledError as exc:
        raise chat_orchestrator.ChatStoppedError("chat turn stopped") from exc
    except action_files.FileRpcBusyError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "another file-bearing Action is still running; retry",
            code="assistant-file-busy",
        ) from exc


def _run_chat_segment(
    self,
    request: SegmentRequest,
) -> chat_turn_engine.SegmentResult:
    added: tuple[str, ...] = ()
    if request.routine is None and request.continuation is None:
        # Recorded before the Brain start can reference them, so a deletion racing this turn purges its thread.
        added = self._turn_started(request.team_id, request.file_ids)
    try:
        with self.storage.metadata_connection(request.team_id, request.file_ids) as metadata_connection:
            result = self._run_chat_segment_with_metadata(request, metadata_connection)
    except Exception:
        # A failed turn leaves no continuation, so nothing can read the files only it referenced.
        self._turn_failed(request.team_id, added)
        raise
    if request.routine is None and isinstance(result.outcome, chat_orchestrator.ChatOutcome):
        self._turn_completed(request.team_id, request.file_ids)
    return result


def _run_chat_segment_with_metadata(
    self,
    request: SegmentRequest,
    metadata_connection,
) -> chat_turn_engine.SegmentResult:
    bindings: dict[str, _ActiveAssistant] = {}
    identity: tuple[object, ...] = ()
    network_id = ""
    contracts: tuple[tuple[str, str], ...] = ()
    selected_files: dict[str, action_files.ActionFile] = {}

    def execute_action(
        action_request: brain_runtime_client.ActionRequest, private_inputs: object, operation_id: str
    ) -> object:
        active = _required_active_assistant(bindings, action_request.assistant_id)
        transcript = action_human.transcript_for(request.transcripts, action_request.interrupt_id)
        if not isinstance(private_inputs, action_execution.RpcPrivateInputs):
            raise action_journal.ActionJournalConflictError("Action private input evidence is unavailable")
        evidence = action_execution.ActionInvocationEvidence(
            private_inputs,
            transcript,
            action_execution.stored_input_origin(action_request),
            operation_id,
            protect=_protector(self, request),
        )

        def invoke() -> object:
            return self._invoke_chat_action(
                request.team_id, request.token, action_request, active.container_id, evidence
            )

        if request.routine is None and request.recording is not None:
            # A recording turn keeps every successful call, and protects what it was given and returned (ADR-0101).
            call = (active, action_request, evidence, routine_diagnostics.protected(evidence))
            return routine_recorder.recorded(
                self.routine_recordings, (request.team_id, request.recording), call, invoke
            )
        if request.routine is None:
            return invoke()
        # A compiled run's cursor names this logical operation and its exact input before the RPC (ADR-0092).
        request.routine.runtime.dispatching(action_request, operation_id, active.container_id, evidence)
        return _observed(invoke, request.routine.runtime, action_request, evidence)

    def human_requirement(
        action_request: brain_runtime_client.ActionRequest,
        human_request: action_human.HumanRequest,
        locale: str,
    ) -> action_challenges.HumanRequirement:
        return _human_requirement(self, bindings, action_request, human_request, locale, selected_files)

    def prepare() -> chat_turn_engine.PreparedSegment:
        nonlocal bindings, identity, network_id, contracts, selected_files
        team_name, network_id, assistants, files, config = self._chat_setup(
            request.team_id,
            request.file_ids,
            request.provider,
            request.assistant_ids,
            metadata_connection,
        )
        # Identity keeps the immutable creation name, so a rename never changes it; the Brain and the terminal get
        # the current display name of this exact network incarnation (ADR-0088).
        identity = self._chat_identity(team_name, network_id, assistants, files, config)
        display_name = self.team_names.load(request.team_id, network_id) or team_name
        routine = request.routine
        generation, thread_id = network_id, _brain_thread_id(self.space_id, request.team_id, network_id)
        if routine is not None:
            generation = routine.generation
            thread_id = _routine_thread_id(self.space_id, request.team_id, network_id, routine.run_id)
            if routine_record.network_of(generation, routine.run_id) != network_id:
                # The Team network changed since the run bound its generation: nothing may run in another network.
                self._raise_chat_problem("context-changed", None)
        if request.continuation is None:
            try:
                self.action_state.end_settled(generation)
            except action_journal.ActionJournalError as exc:
                self._raise_chat_problem("drive-error", exc)
        genesis_by_id = {active.spec.assistant_id: self._active_assistant_genesis(active) for active in assistants}
        contracts = tuple(
            sorted(
                (active.spec.assistant_id, routine_scope(active, genesis_by_id[active.spec.assistant_id]))
                for active in assistants
            )
        )
        context = _turn_context(
            self, request, _TurnScope(thread_id, display_name, assistants, genesis_by_id, files, config)
        )
        bindings = {active.spec.assistant_id: active for active in assistants}
        selected_files = action_files.selected(context.attachments)
        held = routine is not None and routine.held
        batch = (action_execution.HeldActionBatch if held else action_execution.ActionBatch)(
            self.action_state,
            generation,
            context.thread_id,
            bindings,
            action_execution.ActionBatchStrategy(
                lambda active: (active.container_id, active.spec.image),
                execute_action,
                lambda action_request: self._require_action_rpc_envelope(
                    request.team_id,
                    bindings,
                    action_request,
                    selected_files,
                ),
                lambda action_request: self._action_integration_generations(
                    request.team_id,
                    _required_active_assistant(bindings, action_request.assistant_id),
                    action_request.action,
                ),
                lambda action_request, origins: self._action_stored_input_generations(
                    request.team_id,
                    _required_active_assistant(bindings, action_request.assistant_id),
                    action_request,
                    origins,
                ),
                lambda action_request: (
                    _required_active_assistant(bindings, action_request.assistant_id)
                    .spec.actions[action_request.action]
                    .effect
                ),
                # A Routine retry repeats its carried logical operation; anything else lets the journal mint one.
                (lambda _request: None) if routine is None else routine.runtime.logical_operation,
                functools.partial(_admitted_delivery, self, request, bindings),
                stopped=lambda: self._chat_cancelled(request.token),
            ),
        )
        if held:
            routine.batches.append(batch)
        return chat_turn_engine.PreparedSegment(display_name, identity, context, files, batch)

    def private_inputs(
        requests: tuple[object, ...],
        requirements: chat_turn_engine.SegmentRequirements,
    ) -> bool:
        return self._require_chat_private_inputs(request.team_id, bindings, requests, requirements)

    def validate_current_context() -> None:
        self._validate_chat_context(
            request.team_id,
            request.file_ids,
            request.provider,
            request.assistant_ids,
            identity,
            metadata_connection,
        )

    team_name, identity, outcome, requirements = chat_turn_engine.run_segment(
        chat_turn_engine.SegmentStrategy(
            runtime=_runtime(self, request),
            prepare=prepare,
            validate_action=lambda assistant_id, action, payload: self._validate_chat_action(
                bindings,
                assistant_id,
                action,
                payload,
            ),
            pause_for_private_inputs=private_inputs,
            cancelled=lambda: self._chat_cancelled(request.token),
            validate_context=validate_current_context,
            raise_problem=self._raise_chat_problem,
            human_requirement=human_requirement,
            progress=request.progress,
            **({} if request.routine is None else request.routine.runtime.strategy()),
        ),
        message=request.message,
        continuation=request.continuation,
        conversation=request.conversation,
        expected_identity=request.expected_identity,
    )
    return chat_turn_engine.SegmentResult(
        team_name,
        identity,
        outcome,
        requirements.integrations,
        requirements.human,
        contracts,
        request.locale,
        paused_batch=requirements.paused_batch,
    )
