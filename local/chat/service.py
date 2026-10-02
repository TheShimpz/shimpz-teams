"""The Local chat-turn service: turns, continuations, challenges, and the Team execution slot."""

from __future__ import annotations

import secrets
import threading
import time
import weakref
from contextlib import contextmanager
from http import HTTPStatus

from action import challenges as action_challenges
from inference import client as brain_runtime_client
from local.chat import api as local_chat_api
from local.chat import capabilities as local_chat_capabilities
from local.chat import execution as local_chat_execution
from local.chat import human as local_chat_human
from local.chat import pause as local_chat_pause
from local.chat import private as local_chat_private
from local.chat import resume as local_chat_resume
from local.chat import segment as local_chat_segment
from local.chat import state as local_chat_state
from local.composition import ChatTurnDependencies
from local.errors import ApiProblemError as ApiProblem
from local.routine import card as local_routine_card
from local.routine import compiled as local_routine_compiled
from local.routine import diagnostics as local_routine_diagnostics
from local.routine import human as local_routine_human
from local.routine import incident as local_routine_incident
from local.routine import lineage as local_routine_lineage
from local.routine import manage as local_routine_manage
from local.routine import notices as local_routine_notices
from local.routine import question as local_routine_question
from local.routine import recovery as local_routine_recovery
from local.routine import run as local_routine_run
from local.routine import turn as local_routine_turn


class ChatTurnService:
    """Own local chat turns, continuations, challenges, and private state."""

    def __init__(self, dependencies: ChatTurnDependencies) -> None:
        self.space_id = dependencies.space_id
        self.registry = dependencies.registry
        self.storage = dependencies.storage
        self.inference_store = dependencies.inference_store
        self.team_names = dependencies.team_names
        self.brain_runtime = dependencies.brain_runtime
        self.action_state = dependencies.action_state
        self.assistant_integrations = dependencies.assistant_integrations
        self.assistant_stored_inputs = dependencies.assistant_stored_inputs
        self.integration_challenges = dependencies.integration_challenges
        self.human_challenges = dependencies.human_challenges or action_challenges.HumanChallengeStore()
        self.oauth_pkce = dependencies.oauth_pkce
        self.oauth_service = dependencies.oauth_service
        self.chat_continuations = dependencies.chat_continuations
        self.routine_store = dependencies.routine_store
        self.routine_diagnostics = dependencies.routine_diagnostics
        self.routine_lineage = dependencies.routine_lineage or local_routine_lineage.LineageBook()
        # Recovery cards of held runs, each answerable once by the person it was opened for (ADR-0092).
        self.routine_cards = local_routine_card.CardBook()
        # Routine challenges live apart from chat's one per Team, so a frozen run never blocks chat (ADR-0086).
        self.routine_human_challenges = dependencies.routine_human_challenges or action_challenges.HumanChallengeStore()
        self._lock = dependencies.lock_for
        self._raise_storage_problem = dependencies.raise_storage_problem
        self._active_chat_guard = threading.Lock()
        # Held weakly, as in Hosted: a lock lives only while a holder or waiter references it, so looking up any
        # Team id never grows this map, and everyone contending on a Team shares the same lock.
        self._chat_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
        self._active_chat_tokens: dict[str, str] = {}
        self._active_action_containers: dict[str, tuple[str, object]] = {}
        self._cancelled_chat_tokens: set[str] = set()
        self._brain_aborts: dict[str, brain_runtime_client.RequestAbort] = {}
        # The Routine whose run segment holds the Team's execution slot, so a chat message is told why it waits.
        self._routine_holders: dict[str, str] = {}
        # Each running Routine run's Team, execution-slot token, and active-time deadline, for its exact Stop.
        self._routine_runs: dict[str, object] = {}
        # Leased runs a Stop is ending before any worker registered them; a worker registering meanwhile is refused.
        self._routine_halting: set[str] = set()
        # When a chat message last found its Team's slot held by a Routine: chat goes first at the next boundary.
        self._chat_demand: dict[str, float] = {}

    def _chat_lock(self, team_id: str) -> threading.Lock:
        with self._active_chat_guard:
            lock = self._chat_locks.get(team_id)
            if lock is None:
                lock = threading.Lock()
                self._chat_locks[team_id] = lock
            return lock

    def _chat_cancelled(self, token: str) -> bool:
        with self._active_chat_guard:
            return token in self._cancelled_chat_tokens

    def _commit_chat_terminal(self, team_id: str, token: str, before_commit=lambda: None) -> bool:
        """Commit a reply only when Stop did not win this service-owned turn.

        ``before_commit`` runs under the same guard, so its effect happens exactly when the reply commits; if it fails,
        the turn stays uncommitted.
        """
        with self._active_chat_guard:
            if token in self._cancelled_chat_tokens or self._active_chat_tokens.get(team_id) != token:
                return False
            before_commit()
            self._active_chat_tokens.pop(team_id, None)
            return True

    def _cancel_chat_for_destroy(self, team_id: str) -> None:
        """Prevent another Action and synchronously stop one already executing."""
        with self._active_chat_guard:
            token = self._active_chat_tokens.get(team_id)
            if token is not None:
                self._cancelled_chat_tokens.add(token)
            active = self._active_action_containers.get(team_id)
            active_action = active[1] if token is not None and active is not None and active[0] == token else None
            brain_abort = self._brain_aborts.get(token) if token is not None else None
        if brain_abort is not None:
            brain_abort.abort()
        if active_action is not None:
            self.assistant_lifecycle._fail_stop_action(active_action)

    @contextmanager
    def _exclusive_chat_turn(self, team_id: str, routine_id: str | None = None):
        """Hold the Team's one execution slot: a chat turn, or with ``routine_id`` a Routine run's segment."""
        lock = self._chat_lock(team_id)
        if not lock.acquire(blocking=False):
            with self._active_chat_guard:
                routine = team_id in self._routine_holders
                if routine and routine_id is None:
                    # A person waits on a Routine: no further run of the Team starts until chat had its turn.
                    self._chat_demand[team_id] = time.monotonic()
            if routine:
                raise ApiProblem(HTTPStatus.CONFLICT, "Team is running a Routine", code="routine-active")
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team already has an active chat turn",
                code="chat-active",
            )
        token = secrets.token_hex(16)
        # Registered before any Brain request of the turn, so Stop can always reach the one in flight (ADR-0079).
        brain_abort = brain_runtime_client.RequestAbort()
        with self._active_chat_guard:
            self._active_chat_tokens[team_id] = token
            self._brain_aborts[token] = brain_abort
            if routine_id is not None:
                self._routine_holders[team_id] = routine_id
            else:
                self._chat_demand.pop(team_id, None)
        try:
            with brain_runtime_client.abortable(brain_abort):
                yield token
        finally:
            with self._active_chat_guard:
                self._brain_aborts.pop(token, None)
                if routine_id is not None:
                    self._routine_holders.pop(team_id, None)
                if self._active_chat_tokens.get(team_id) == token:
                    self._active_chat_tokens.pop(team_id, None)
                active = self._active_action_containers.get(team_id)
                if active is not None and active[0] == token:
                    self._active_action_containers.pop(team_id, None)
                self._cancelled_chat_tokens.discard(token)
            lock.release()

    _pending_chat_continuation = local_chat_api._pending_chat_continuation
    _segment_response = local_chat_api._segment_response
    chat = local_chat_api.chat
    action_labels = local_chat_capabilities.action_labels
    _action_label_snapshot = local_chat_capabilities._action_label_snapshot
    capability_plan = local_chat_capabilities.capability_plan
    _capability_plan_snapshot = local_chat_capabilities._capability_plan_snapshot
    intent_route = local_chat_capabilities.intent_route
    resume_chat_integrations = local_chat_api.resume_chat_integrations
    resume_chat_human = local_chat_human.resume_chat_human
    pending_chat_human = local_chat_human.pending_chat_human
    open_chat_human = local_chat_human.open_chat_human
    _relocalized_human = local_chat_human.relocalized
    _expire_human_challenges = local_chat_human._expire_human_challenges
    _chat_routines = local_routine_turn.chat_routines
    _routine_change = local_routine_turn.admit_change
    _routine_question = local_routine_question.admit
    _recover_routine_run = local_routine_recovery.automatic
    open_routine_card = local_routine_card.open_card
    answer_routine_card = local_routine_card.answer_card
    resume_routine = local_routine_incident.resume_routine
    claim_routine_run = local_routine_run.claim_routine_run
    next_routine_due = local_routine_run.next_routine_due
    run_routine = local_routine_compiled.run_routine
    _stop_routine_run = local_routine_run.halt_routine_run
    list_routines = local_routine_manage.list_routines
    delete_routine = local_routine_manage.delete_routine
    open_routine_challenge = local_routine_human.open_routine_challenge
    resume_routine_human = local_routine_human.resume_routine_human
    resume_routine_integrations = local_routine_human.resume_routine_integrations
    current_routine_challenge = local_routine_human.current_routine_challenge
    _cancel_routine_challenge = local_routine_human.cancel_routine_challenge
    routine_notices = local_routine_notices.routine_notices
    acknowledge_routine_notices = local_routine_notices.acknowledge_notices
    stop_routine = local_routine_notices.stop_routine
    routine_run_diagnostics = local_routine_diagnostics.run_diagnostics

    _invoke_chat_action = local_chat_execution._invoke_chat_action
    _chat_identity = staticmethod(local_chat_execution._chat_identity)
    _raise_chat_problem = staticmethod(local_chat_execution._raise_chat_problem)
    _validate_chat_action = staticmethod(local_chat_execution._validate_chat_action)
    _require_chat_private_inputs = local_chat_execution._require_chat_private_inputs
    _validate_chat_context = local_chat_execution._validate_chat_context

    _commit_suspension = local_chat_pause._commit_suspension
    _integration_response = local_chat_pause._integration_response
    _human_response = local_chat_pause._human_response
    _purge_human_generation = local_chat_pause._purge_human_generation
    _purge_human_pending = local_chat_pause._purge_human_pending
    _terminal_human_failure = local_chat_pause._terminal_human_failure
    _pause_integration = local_chat_pause._pause_integration
    _pause_human = local_chat_pause._pause_human

    _action_integration_generations = local_chat_private._action_integration_generations
    _action_stored_input_generations = local_chat_private._action_stored_input_generations
    _refresh_oauth_integration = local_chat_private._refresh_oauth_integration
    _resolve_action_integrations = local_chat_private._resolve_action_integrations
    _resolve_action_stored_inputs = local_chat_private._resolve_action_stored_inputs
    _require_action_rpc_envelope = local_chat_private._require_action_rpc_envelope
    _raise_integration_problem = staticmethod(local_chat_private._raise_integration_problem)
    _raise_stored_input_problem = staticmethod(local_chat_private._raise_stored_input_problem)
    list_assistant_integrations = local_chat_private.list_assistant_integrations
    list_assistant_stored_inputs = local_chat_private.list_assistant_stored_inputs
    clear_assistant_stored_input = local_chat_private.clear_assistant_stored_input
    start_assistant_integration_authorization = local_chat_private.start_assistant_integration_authorization
    _current_integration_declaration = local_chat_private._current_integration_declaration
    complete_cloudflare_oauth_callback = local_chat_private.complete_cloudflare_oauth_callback
    cancel_assistant_integration_authorization = local_chat_private.cancel_assistant_integration_authorization
    disconnect_assistant_integration = local_chat_private.disconnect_assistant_integration
    pending_chat_integrations = local_chat_private.pending_chat_integrations

    stop_chat = local_chat_resume.stop_chat

    _run_chat_segment = local_chat_segment._run_chat_segment
    _run_chat_segment_with_metadata = local_chat_segment._run_chat_segment_with_metadata

    _chat_file_metadata = local_chat_state._chat_file_metadata
    _chat_setup = local_chat_state._chat_setup
    _team_assistants = local_chat_state._team_assistants

    def _active_assistant_genesis(self, active):
        return self.assistant_lifecycle._active_assistant_genesis(active)

    def _assistant_language(self, active):
        return self.assistant_lifecycle._assistant_language(active)

    def _admit_assistant_allowed_hosts(self, container, spec):
        return self.assistant_lifecycle._admit_assistant_allowed_hosts(container, spec)

    _active_chat_assistants = local_chat_state._active_chat_assistants
    _delete_assistant_integration_state = local_chat_state._delete_assistant_integration_state
    _delete_assistant_stored_input_state = local_chat_state._delete_assistant_stored_input_state
    _delete_team_integration_state = local_chat_state._delete_team_integration_state
    _delete_team_stored_input_state = local_chat_state._delete_team_stored_input_state
    _delete_all_integration_state = local_chat_state._delete_all_integration_state
    _delete_all_stored_input_state = local_chat_state._delete_all_stored_input_state
    _retain_declared_assistant_integration_state = local_chat_state._retain_declared_assistant_integration_state
    _retain_declared_assistant_stored_input_state = local_chat_state._retain_declared_assistant_stored_input_state
    _raise_chat_continuation_problem = staticmethod(local_chat_state._raise_chat_continuation_problem)
    _persist_chat_continuation = local_chat_state._persist_chat_continuation
    _restore_chat_continuation = local_chat_state._restore_chat_continuation
    _purge_expired_human_continuation = local_chat_state._purge_expired_human_continuation
    _restore_all_chat_continuations = local_chat_state._restore_all_chat_continuations
    _delete_chat_continuation = local_chat_state._delete_chat_continuation
    _clear_chat_continuations = local_chat_state._clear_chat_continuations
