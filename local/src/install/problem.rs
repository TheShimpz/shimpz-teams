//! The public problems of the Assistants group, each with one status, message, and code.

use crate::http::service::Problem;

macro_rules! problems {
    ($($(#[$doc:meta])* $name:ident = ($status:expr, $message:expr, $code:expr);)*) => {
        $($(#[$doc])* pub const $name: Problem = Problem::new($status, $message, $code);)*
    };
}

problems! {
    /// The Team id is not one.
    INVALID_TEAM_ID = (422, "invalid Team id", "invalid-team-id");
    /// The Assistant id is not one.
    INVALID_ASSISTANT_ID = (422, "invalid Assistant id", "invalid-assistant-id");
    /// The locale is not one interface language.
    INVALID_LOCALE = (422, "locale must be one interface language", "invalid-locale");
    /// The install body is not exactly `{assistant_id, source_digest}`.
    INSTALL_BODY = (422, "Assistant installation requires assistant_id and source_digest", "invalid-body");
    /// The Local install body is not exactly `{image_id}`.
    LOCAL_INSTALL_BODY = (422, "Local Assistant installation requires image_id", "invalid-body");
    /// The Team has no binding of the Assistant.
    NOT_INSTALLED = (404, "Assistant is not installed in this Team", "assistant-not-installed");
    /// The Team already has the most Assistants.
    LIMIT_REACHED = (409, "Team already has the maximum number of Assistants", "assistant_limit_reached");
    /// The registry refused the binding.
    BINDING_CONFLICT = (409, "Assistant publication binding failed", "assistant-binding-conflict");
    /// The Local binding was refused.
    LOCAL_BINDING_CONFLICT = (409, "Local Assistant binding failed", "assistant-binding-conflict");
    /// An Assistant mutation while the Team's chat turn holds its slot.
    CHAT_ACTIVE = (409, "Assistant lifecycle cannot change during an active Team chat turn", "chat-active");
    /// The icon store failed.
    ICON_UNAVAILABLE = (503, "Assistant icon storage is unavailable", "assistant-icon-unavailable");
    /// A binding's icon cannot be read.
    ICON_READ_UNAVAILABLE = (503, "Assistant icon is unavailable", "assistant-icon-unavailable");
    /// The image's manifest or contract differs from its review.
    MANIFEST_INVALID = (409, "installed Assistant manifest failed its reviewed contract", "assistant-manifest-invalid");
    /// The image's manifest could not be read.
    MANIFEST_UNAVAILABLE = (503, "installed Assistant manifest could not be verified", "assistant-manifest-unavailable");
    /// The publication is not installable.
    NOT_INSTALLABLE = (404, "Assistant publication is not installable", "assistant-not-installable");
    /// Developers answered outside the contract.
    DEVELOPERS_PROTOCOL = (502, "Developers response violates the Assistant publication contract", "developers-protocol-invalid");
    /// Developers could not be reached.
    DEVELOPERS_UNAVAILABLE = (503, "Developers is unavailable", "developers-unavailable");
    /// The artifact's trust could not be proved.
    UNTRUSTED = (409, "Assistant artifact trust failed", "assistant-artifact-untrusted");
    /// An automatic update met another binding.
    UPDATE_CONFLICT = (409, "Assistant binding changed during update", "assistant-update-conflict");
    /// An automatic update widens egress or moves a credential.
    UPDATE_APPROVAL_REQUIRED = (409, "Assistant update requires approval for expanded outbound hosts or moved credentials", "assistant-update-approval-required");
    /// The update could not restore the previous generation.
    UPDATE_ROLLBACK_INCOMPLETE = (500, "Assistant update rollback is incomplete", "assistant-update-rollback-incomplete");
    /// Docker could not replace the Assistant.
    REPLACE_FAILED = (503, "Docker could not replace the Assistant", "docker-remove-failed");
    /// Docker could not keep the current image.
    IMAGE_UNAVAILABLE = (503, "Docker could not preserve the current Assistant image", "docker-image-unavailable");
    /// The staged snapshot inventory could not be read.
    SNAPSHOTS_UNAVAILABLE = (503, "Local Assistant snapshots are unavailable", "local-assistant-snapshots-unavailable");
    /// The staged snapshot inventory is invalid.
    SNAPSHOTS_INVALID = (409, "Local Assistant snapshot inventory is invalid", "local-assistant-snapshots-invalid");
    /// The preview capacity is busy.
    PREVIEW_BUSY = (503, "Local Assistant preview capacity is busy", "local-assistant-preview-busy");
    /// The preview could not be read.
    PREVIEW_UNAVAILABLE = (503, "Local Assistant preview is unavailable", "local-assistant-preview-unavailable");
    /// The preview failed admission.
    PREVIEW_INVALID = (409, "Local Assistant preview failed admission", "local-assistant-preview-invalid");
    /// The snapshot could not be read.
    SNAPSHOT_UNAVAILABLE = (503, "Local Assistant snapshot is unavailable", "local-assistant-snapshot-unavailable");
    /// The snapshot failed admission.
    SNAPSHOT_INVALID = (409, "Local Assistant snapshot failed admission", "local-assistant-snapshot-invalid");
    /// The Action is not declared.
    ACTION_NOT_DECLARED = (404, "Action is not declared", "action-not-declared");
    /// The Action input does not match its schema.
    ACTION_INPUT_INVALID = (422, "Action payload does not match its reviewed schema", "invalid-action-input");
    /// The Team's capabilities changed.
    CONTEXT_CHANGED = (409, "Team capabilities changed; retry", "team-context-changed");
    /// The direct route cannot ask a person.
    HUMAN_REQUEST_REQUIRED = (409, "the Action asks a person to answer, which only a Team chat can collect", "action-human-request-required");
    /// Stop arrived before dispatch.
    ACTION_STOPPED = (409, "the Action was stopped before it ran", "chat-stopped");
    /// The exchange failed after dispatch.
    ACTION_UNCERTAIN = (502, "the Assistant Action did not complete", "assistant-rpc-failed");
    /// The Action answered a closed failure.
    ACTION_FAILED = (502, "the Assistant Action failed", "assistant-action-failed");
    /// Action state is unavailable.
    ACTION_STATE_UNAVAILABLE = (503, "Team Action execution state is unavailable", "action-state-unavailable");
}
