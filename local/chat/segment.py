"""Local chat segment orchestration operations."""

from dataclasses import dataclass, field

from action import challenges as action_challenges
from action import execution as action_execution
from action import human as action_human
from action import journal as action_journal
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import required_active_assistant as _required_active_assistant
from local.validation import brain_thread_id as _brain_thread_id


@dataclass(frozen=True, slots=True)
class SegmentRequest:
    team_id: str
    file_ids: list[str]
    assistant_ids: tuple[str, ...]
    provider: str
    api_key: str
    token: str
    message: str | None = None
    continuation: chat_orchestrator.ChatContinuation | None = None
    expected_identity: tuple[object, ...] | None = None
    transcripts: tuple[action_human.ActionTranscript, ...] = ()
    progress: chat_progress.Reporter = field(default_factory=chat_progress.Reporter)


def _run_chat_segment(
    self,
    request: SegmentRequest,
) -> chat_turn_engine.SegmentResult:
    with self.storage.metadata_connection(request.team_id, request.file_ids) as metadata_connection:
        return self._run_chat_segment_with_metadata(request, metadata_connection)


def _run_chat_segment_with_metadata(
    self,
    request: SegmentRequest,
    metadata_connection,
) -> chat_turn_engine.SegmentResult:
    bindings: dict[str, _ActiveAssistant] = {}
    identity: tuple[object, ...] = ()
    network_id = ""

    def execute_action(action_request: brain_runtime_client.ActionRequest, private_inputs: object) -> object:
        active = _required_active_assistant(bindings, action_request.assistant_id)
        transcript = action_human.transcript_for(request.transcripts, action_request.interrupt_id)
        if not isinstance(private_inputs, action_execution.RpcPrivateInputs):
            raise action_journal.ActionJournalConflictError("Action private input evidence is unavailable")
        return self._invoke_chat_action(
            request.team_id,
            request.token,
            action_request,
            active.container_id,
            transcript,
            private_inputs,
        )

    def human_requirement(
        action_request: brain_runtime_client.ActionRequest,
        human_request: action_human.HumanRequest,
    ) -> action_challenges.HumanRequirement:
        active = _required_active_assistant(bindings, action_request.assistant_id)
        action = active.spec.actions.get(action_request.action)
        if action is None:
            raise chat_orchestrator.ChatOrchestrationError("Action human request contract changed")
        return action_challenges.HumanRequirement(
            active.spec.assistant_id,
            active.spec.name,
            action_request.action,
            action.summary,
            action_request.interrupt_id,
            human_request,
            active.spec.version,
        )

    def prepare() -> chat_turn_engine.PreparedSegment:
        nonlocal bindings, identity, network_id
        team_name, network_id, assistants, files, config = self._chat_setup(
            request.team_id,
            request.file_ids,
            request.provider,
            request.assistant_ids,
            metadata_connection,
        )
        identity = self._chat_identity(team_name, network_id, assistants, files, config)
        if request.continuation is None:
            try:
                self.action_state.purge_replayable(network_id)
            except action_journal.ActionJournalError as exc:
                self._raise_chat_problem("drive-error", exc)
        genesis_by_id = {active.spec.assistant_id: self._active_assistant_genesis(active) for active in assistants}
        context = brain_runtime_client.RuntimeContext(
            thread_id=_brain_thread_id(self.space_id, request.team_id, network_id),
            team_name=team_name,
            assistants=tuple(
                brain_runtime_client.RuntimeAssistant(
                    id=active.spec.assistant_id,
                    genesis=genesis_by_id[active.spec.assistant_id],
                    actions=tuple(
                        brain_runtime_client.RuntimeAction(
                            id=action_id,
                            summary=action.summary,
                            input_schema=action.input_schema,
                        )
                        for action_id, action in sorted(active.spec.actions.items())
                    ),
                )
                for active in assistants
            ),
            provider=config.provider,
            model=config.model,
            api_key=request.api_key,
        )
        bindings = {active.spec.assistant_id: active for active in assistants}
        batch = action_execution.ActionBatch(
            self.action_state,
            network_id,
            context.thread_id,
            bindings,
            action_execution.ActionBatchStrategy(
                lambda active: (active.container_id, active.spec.image),
                execute_action,
                lambda action_request: self._require_action_rpc_envelope(
                    request.team_id,
                    bindings,
                    action_request,
                ),
                lambda action_request: self._action_integration_generations(
                    request.team_id,
                    _required_active_assistant(bindings, action_request.assistant_id),
                    action_request.action,
                ),
                lambda action_request: self._action_stored_input_generations(
                    request.team_id,
                    _required_active_assistant(bindings, action_request.assistant_id),
                    action_request,
                ),
            ),
        )
        return chat_turn_engine.PreparedSegment(team_name, identity, context, files, batch)

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
            runtime=self.brain_runtime,
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
        ),
        message=request.message,
        continuation=request.continuation,
        expected_identity=request.expected_identity,
    )
    return chat_turn_engine.SegmentResult(
        team_name,
        identity,
        outcome,
        requirements.integrations,
        requirements.human,
    )
