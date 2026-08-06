"""Local chat stop API operation."""

from local.validation import validate_team_id


def stop_chat(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    network = self.assistant_lifecycle._network(team_id)
    integration_cancelled = self.integration_challenges.cancel_team(team_id)
    human_cancelled = self.human_challenges.cancel_team(team_id)
    self.oauth_pkce.cancel_team(team_id)
    continuation_cancelled = self._delete_chat_continuation(team_id)
    if human_cancelled:
        self._purge_human_generation(network.id)
    power_stopped = False
    active_power = None
    with self._active_chat_guard:
        token = self._active_chat_tokens.get(team_id)
        if token is not None:
            self._cancelled_chat_tokens.add(token)
        active = self._active_power_containers.get(team_id)
        if token is not None and active is not None and active[0] == token:
            active_power = active[1]
    if active_power is not None:
        self.assistant_lifecycle._fail_stop_power(active_power)
        power_stopped = True
    accepted = token is not None or integration_cancelled or human_cancelled or continuation_cancelled
    return {
        "team_id": team_id,
        "requested": accepted,
        "accepted": accepted,
        "confirmed": power_stopped,
        "forced_restart": False,
    }
