"""Local chat stop API operation."""

from local.validation import validate_team_id


def stop_chat(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    # Under the Team lock, so a relocalization's reissue and persist, or an OAuth start, never straddles the withdrawal.
    # Only leaf locks are taken inside; the lock is released before cleanup and the interruption of the running turn.
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        # The token is cancelled before any challenge is withdrawn: a pause commits only while its token is current, so
        # it either committed already and its challenge is withdrawn below, or its commit fails and rolls it back.
        with self._active_chat_guard:
            # A Routine run holding the Team's slot has its own exact Stop; chat Stop never reaches it (ADR-0086).
            token = None if team_id in self._routine_holders else self._active_chat_tokens.get(team_id)
            if token is not None:
                self._cancelled_chat_tokens.add(token)
            active = self._active_action_containers.get(team_id)
            active_action = active[1] if token is not None and active is not None and active[0] == token else None
            brain_abort = self._brain_aborts.get(token) if token is not None else None
        integration = self.integration_challenges.withdraw_team(team_id)
        human = self.human_challenges.withdraw_team(team_id)
        self.oauth_pkce.cancel_team(team_id)
    # A failed cleanup is still reported, but only after the cancelled turn is interrupted: it must never keep an
    # Action running.
    try:
        continuation_cancelled = _end_withdrawn(self, team_id, integration, human)
    finally:
        _interrupt_turn(self, brain_abort, active_action)
    accepted = token is not None or integration is not None or human is not None or continuation_cancelled
    return {
        "team_id": team_id,
        "requested": accepted,
        "accepted": accepted,
        "confirmed": active_action is not None,
        "forced_restart": False,
    }


def _end_withdrawn(self, team_id: str, integration: object | None, human: object | None) -> bool:
    """End what Stop withdrew; report whether a continuation was deleted."""
    # Only the continuations Stop withdrew, each deleted: a turn paused since keeps its own.
    deleted = [self._delete_withdrawn_continuation(team_id, item) for item in (integration, human) if item is not None]
    if human is not None:
        # Only the paused turn's own batch: a turn started since keeps its batch (ADR-0038).
        self._purge_human_pending(human.payload)
    return True in deleted


def _interrupt_turn(self, brain_abort: object | None, active_action: object | None) -> None:
    # The token is already cancelled, so the aborted Brain request resolves as a stopped turn, never a failure.
    if brain_abort is not None:
        brain_abort.abort()
    if active_action is not None:
        self.assistant_lifecycle._fail_stop_action(active_action)
