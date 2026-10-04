"""Local chat stop API operation."""

from local.validation import validate_team_id


def stop_chat(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    # Under the Team lock, so a relocalization's reissue and persist never straddle the withdrawal. Only leaf locks are
    # taken inside, and the lock is released before cleanup and the interruption of the running turn.
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        integration = self.integration_challenges.withdraw_team(team_id)
        human = self.human_challenges.withdraw_team(team_id)
    self.oauth_pkce.cancel_team(team_id)
    # Only the continuations Stop withdrew, each deleted: a turn paused since keeps its own.
    deleted = [self._delete_withdrawn_continuation(team_id, item) for item in (integration, human) if item is not None]
    continuation_cancelled = True in deleted
    integration_cancelled = integration is not None
    human_cancelled = human is not None
    if human_cancelled:
        # Only the paused turn's own batch: a turn started since keeps its batch (ADR-0038).
        self._purge_human_pending(human.payload)
    action_stopped = False
    active_action = None
    with self._active_chat_guard:
        # A Routine run holding the Team's slot has its own exact Stop; chat Stop never reaches it (ADR-0086).
        token = None if team_id in self._routine_holders else self._active_chat_tokens.get(team_id)
        if token is not None:
            self._cancelled_chat_tokens.add(token)
        active = self._active_action_containers.get(team_id)
        if token is not None and active is not None and active[0] == token:
            active_action = active[1]
        brain_abort = self._brain_aborts.get(token) if token is not None else None
    # The token is already cancelled, so the aborted Brain request resolves as a stopped turn, never a failure.
    if brain_abort is not None:
        brain_abort.abort()
    if active_action is not None:
        self.assistant_lifecycle._fail_stop_action(active_action)
        action_stopped = True
    accepted = token is not None or integration_cancelled or human_cancelled or continuation_cancelled
    return {
        "team_id": team_id,
        "requested": accepted,
        "accepted": accepted,
        "confirmed": action_stopped,
        "forced_restart": False,
    }
