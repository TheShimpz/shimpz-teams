//! The public failures of chat and Routines (`local/errors.py`): each carries one status, fixed message, and stable
//! code, which Admin localizes.

use crate::http::service::Problem;

macro_rules! problems {
    ($($(#[$meta:meta])* $name:ident = ($status:expr, $message:expr, $code:expr);)*) => {
        $(
            $(#[$meta])*
            #[must_use]
            pub const fn $name() -> Problem {
                Problem::new($status, $message, $code)
            }
        )*
    };
}

problems! {
    /// A selected Assistant's contract or container changed since the turn bound it.
    team_context_changed = (409, "Team capabilities changed; retry", "team-context-changed");
    /// A turn or Routine run names an Assistant the Team does not run.
    assistant_unavailable = (409, "a selected Assistant is unavailable", "assistant-unavailable");
    /// Docker could not answer.
    docker_unavailable = (503, "Docker is unavailable", "docker-unavailable");
    /// A Team resource failed its ownership contract.
    ownership_conflict = (409, "Team resource ownership conflict", "ownership-conflict");
    /// The paused Integration challenge expired.
    integration_challenge_expired = (
        409,
        "Assistant integration request expired; retry the message",
        "assistant-integration-challenge-expired"
    );
    /// Stop won the turn.
    chat_stopped = (409, "chat turn stopped", "chat-stopped");
    /// The Team has no model configuration.
    inference_not_configured = (409, "Team model provider is not configured", "inference-not-configured");
    /// The request's provider is not the Team's.
    inference_provider_mismatch = (409, "configured model provider changed; retry", "inference-provider-mismatch");
    /// The Action journal is unavailable.
    action_state_unavailable = (503, "Team Action execution state is unavailable", "action-state-unavailable");
    /// A selected file is absent.
    selected_file_not_found = (404, "selected file not found", "file-not-found");
    /// An Integration declaration cannot be read.
    integration_contract_unavailable = (
        409,
        "Assistant integration contract is unavailable",
        "assistant-integration-contract-invalid"
    );
    /// The Team's slot did not free in time for a reset or destruction.
    chat_stop_timeout = (409, "active Team chat did not stop in time", "chat-active");
    /// A Routine run holds the Team's slot.
    routine_active = (409, "Team is running a Routine", "routine-active");
    /// A chat turn holds the Team's slot.
    chat_active = (409, "Team already has an active chat turn", "chat-active");
    /// The Space already runs as many chat turns as its limit allows (ADR-0119); Admin keeps the turn queued.
    space_busy = (429, "the Space runs as many chat turns as its limit allows", "space-busy");
    /// The Space is being reset.
    space_resetting = (409, "the Space is being reset", "space-resetting");
    /// The chat limit's record cannot be read or written.
    chat_limit_unavailable = (503, "the Space chat limit is unavailable", "chat-limit-unavailable");
    /// The human request changed.
    human_request_invalid = (409, "Action human request changed; retry the message", "human-request-invalid");
    /// The human request expired.
    human_request_expired = (409, "Action human request expired; retry the message", "human-request-expired");
    /// The human response is not one of the closed answers.
    human_response_invalid = (422, "Action human response is invalid", "invalid-body");
    /// The human response does not match its request.
    human_response_mismatch = (422, "Action human response does not match its request", "invalid-human-response");
    /// An opening names more than its locale.
    challenge_locale_only = (422, "opening a challenge requires only locale", "invalid-body");
    /// The Brain could not complete the turn.
    brain_turn_failed = (502, "Brain could not complete the Team turn", "brain-runtime-failed");
    /// The Brain runtime is unavailable.
    brain_runtime_failed = (502, "Brain runtime is unavailable", "brain-runtime-failed");
    /// A Stored Input store is unavailable.
    stored_input_unavailable = (
        503,
        "Assistant Stored Input state is unavailable",
        "assistant-stored-input-state-unavailable"
    );
    /// The Assistant's Action workload is blocked until it is reinstalled.
    assistant_action_blocked = (
        503,
        "Assistant Action execution is blocked until this Assistant is reinstalled",
        "assistant-action-blocked"
    );
    /// The request's model credential is invalid.
    invalid_model_credential = (422, "private model credential is invalid", "invalid-model-credential");
    /// A body that must be empty was not.
    empty_body_required = (422, "request requires an empty object", "invalid-body");
    /// An internal invariant failed.
    internal_error = (500, "internal error", "internal-error");
    /// The Routine is unavailable.
    routine_not_found = (404, "Routine is unavailable", "routine-not-found");
    /// The Routine's steps are unavailable.
    routine_steps_not_found = (404, "Routine steps are unavailable", "routine-steps-not-found");
    /// The Routine run is unavailable.
    routine_run_not_found = (404, "Routine run is unavailable", "routine-run-not-found");
    /// The Routine run's steps are unavailable.
    routine_run_steps_not_found = (404, "Routine run steps are unavailable", "routine-run-steps-not-found");
    /// The Routine run's lease is not live.
    routine_lease_invalid = (409, "Routine run lease is not live", "routine-lease-invalid");
    /// The Routine incident is unavailable.
    routine_incident_unavailable = (404, "Routine incident is unavailable", "routine-incident-unavailable");
    /// The recovery card is stale.
    routine_card_stale = (409, "the recovery card is stale; open it again", "routine-card-stale");
    /// A Routine card needs a person.
    routine_card_person_required = (403, "a person must answer a Routine card", "routine-card-person-required");
    /// The deliveries an acknowledgement names are invalid.
    routine_deliveries_invalid = (422, "Routine deliveries are invalid", "invalid-body");
    /// The Team's Routine state is unavailable.
    routine_state_unavailable = (503, "Team Routine state is unavailable", "routine-state-unavailable");
    /// Routine diagnostics are unavailable.
    routine_diagnostics_unavailable = (503, "Routine diagnostics are unavailable", "routine-state-unavailable");
}
