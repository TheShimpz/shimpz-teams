//! The controller's exact route table: method, path pattern, operation, and how each operation is authorized,
//! answered, and served.
//!
//! The order is the resolution order: the first route whose method is equal and whose pattern has as many segments
//! as the path, every literal segment equal and every `:name` segment captured, wins. There is no method fallthrough
//! and no wildcard suffix. The HTTP layer (W8) proves the path canonical before it resolves it.

use super::operation::Operation;

/// A request method the controller routes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Method {
    /// `GET`.
    Get,
    /// `POST`.
    Post,
    /// `PUT`.
    Put,
    /// `PATCH`.
    Patch,
    /// `DELETE`.
    Delete,
}

impl Method {
    /// The method of an exact request-line token, if routed.
    #[must_use]
    pub fn parse(token: &str) -> Option<Self> {
        match token {
            "GET" => Some(Self::Get),
            "POST" => Some(Self::Post),
            "PUT" => Some(Self::Put),
            "PATCH" => Some(Self::Patch),
            "DELETE" => Some(Self::Delete),
            _ => None,
        }
    }
}

/// The service group, and so the workstream, that answers an operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Group {
    /// Health, activity, and the Supervisor key rotation (the HTTP and authority layer).
    Controller,
    /// Team lifecycle and the Space reset.
    Teams,
    /// Team files, previews, and downloads.
    Files,
    /// Browser views and Sign-ins.
    Browser,
    /// The Team's inference settings.
    Inference,
    /// Chat turns, their pauses, Stop, and the Space chat limit.
    Chat,
    /// Integrations and Stored Inputs.
    Integrations,
    /// Routines and their runs.
    Routines,
    /// Assistants: inventory, details, install, uninstall, and direct invocation.
    Assistants,
}

/// Who may call an operation, beyond the controller bearer every request carries.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Authority {
    /// The bearer alone (machine calls: health, activity, bootstrap reset, broker completion, Routine delivery).
    Machine,
    /// A Routine assertion bound to the run's lease.
    Routine,
    /// A Supervisor session assertion bound to the request.
    Supervisor,
    /// A Supervisor session assertion, or the CLI's host-reset assertion.
    SupervisorOrHostReset,
}

/// How an operation answers.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Reply {
    /// One bounded JSON document.
    Json,
    /// A chunked NDJSON stream of progress records and one terminal record.
    Ndjson,
    /// Bytes of a declared media type (an image rendition, an icon, or a file's original).
    Binary,
    /// A raw WebSocket upgrade relayed byte for byte.
    Upgrade,
}

/// One route of the table.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Route {
    /// Its method.
    pub method: Method,
    /// Its pattern: `/`-separated segments, `:name` capturing one segment.
    pub pattern: &'static str,
    /// Its operation.
    pub operation: Operation,
    /// The operation's wire and audit name.
    pub name: &'static str,
    /// The group that answers it.
    pub group: Group,
    /// Who may call it.
    pub authority: Authority,
    /// How it answers.
    pub reply: Reply,
}

const fn route(
    method: Method,
    pattern: &'static str,
    operation: Operation,
    name: &'static str,
    group: Group,
    authority: Authority,
    reply: Reply,
) -> Route {
    Route {
        method,
        pattern,
        operation,
        name,
        group,
        authority,
        reply,
    }
}

/// Every route, in resolution order; `ROUTES[operation as usize]` is that operation's route.
pub const ROUTES: [Route; 75] = [
    route(
        Method::Get,
        "/v1/teams",
        Operation::TeamList,
        "team-list",
        Group::Teams,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/oauth/cloudflare/callback",
        Operation::AssistantIntegrationComplete,
        "assistant-integration-complete",
        Group::Integrations,
        Authority::Machine,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/create",
        Operation::TeamCreate,
        "team-create",
        Group::Teams,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/teams/:team_id",
        Operation::TeamDestroy,
        "team-destroy",
        Group::Teams,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Patch,
        "/v1/teams/:team_id",
        Operation::TeamRename,
        "team-rename",
        Group::Teams,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/files",
        Operation::FileList,
        "file-list",
        Group::Files,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/files",
        Operation::FileUpload,
        "file-upload",
        Group::Files,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/files/:file_id/preview",
        Operation::FilePreview,
        "file-preview",
        Group::Files,
        Authority::Supervisor,
        Reply::Binary,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/files/:file_id/pages/:page/preview",
        Operation::FilePagePreview,
        "file-page-preview",
        Group::Files,
        Authority::Supervisor,
        Reply::Binary,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/files/:file_id/content",
        Operation::FileContent,
        "file-content",
        Group::Files,
        Authority::Supervisor,
        Reply::Binary,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/files/:file_id/acknowledgement",
        Operation::FileAcknowledgement,
        "file-acknowledgement",
        Group::Files,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/browser/view",
        Operation::BrowserViewOpen,
        "browser-view-open",
        Group::Browser,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/browser/views/:view_id/stream",
        Operation::BrowserViewStream,
        "browser-view-stream",
        Group::Browser,
        Authority::Supervisor,
        Reply::Upgrade,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/browser/views/:view_id/control",
        Operation::BrowserViewControl,
        "browser-view-control",
        Group::Browser,
        Authority::Supervisor,
        Reply::Upgrade,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/browser/sign-in",
        Operation::BrowserSignInStart,
        "browser-sign-in-start",
        Group::Browser,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/browser/sign-in/save",
        Operation::BrowserSignInSave,
        "browser-sign-in-save",
        Group::Browser,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/browser/sign-in/cancel",
        Operation::BrowserSignInCancel,
        "browser-sign-in-cancel",
        Group::Browser,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/browser/sign-in",
        Operation::BrowserSignIn,
        "browser-sign-in",
        Group::Browser,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/browser/sign-in/delete",
        Operation::BrowserSignInDelete,
        "browser-sign-in-delete",
        Group::Browser,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/inference",
        Operation::InferenceStatus,
        "inference-status",
        Group::Inference,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Put,
        "/v1/teams/:team_id/inference",
        Operation::InferenceConfigure,
        "inference-configure",
        Group::Inference,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/chat",
        Operation::Chat,
        "chat",
        Group::Chat,
        Authority::Supervisor,
        Reply::Ndjson,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/chat/capability-plan",
        Operation::ChatCapabilityPlan,
        "chat-capability-plan",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/chat/intent-route",
        Operation::ChatIntentRoute,
        "chat-intent-route",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/chat/integrations",
        Operation::ChatIntegrationPending,
        "chat-integration-pending",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/chat/integrations",
        Operation::ChatIntegrationSubmit,
        "chat-integration-submit",
        Group::Chat,
        Authority::Supervisor,
        Reply::Ndjson,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/chat/human",
        Operation::ChatHumanPending,
        "chat-human-pending",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/chat/human",
        Operation::ChatHumanSubmit,
        "chat-human-submit",
        Group::Chat,
        Authority::Supervisor,
        Reply::Ndjson,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/chat/human/challenge",
        Operation::ChatHumanOpen,
        "chat-human-open",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/chat/stop",
        Operation::ChatStop,
        "chat-stop",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/chat/limit",
        Operation::ChatLimitRead,
        "chat-limit-read",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Put,
        "/v1/chat/limit",
        Operation::ChatLimitSave,
        "chat-limit-save",
        Group::Chat,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/assistant-integrations",
        Operation::AssistantIntegrationList,
        "assistant-integration-list",
        Group::Integrations,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/assistant-integrations/challenges/:challenge_id/authorize",
        Operation::AssistantIntegrationAuthorize,
        "assistant-integration-authorize",
        Group::Integrations,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/teams/:team_id/assistant-integrations/challenges/:challenge_id/authorize",
        Operation::AssistantIntegrationCancel,
        "assistant-integration-cancel",
        Group::Integrations,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/assistant-stored-inputs",
        Operation::AssistantStoredInputList,
        "assistant-stored-input-list",
        Group::Integrations,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/teams/:team_id/assistant-stored-inputs/:assistant_id/:stored_input_id",
        Operation::AssistantStoredInputClear,
        "assistant-stored-input-clear",
        Group::Integrations,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/routines/claim",
        Operation::RoutineClaim,
        "routine-claim",
        Group::Routines,
        Authority::Machine,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/routines/notices",
        Operation::RoutineNotices,
        "routine-notices",
        Group::Routines,
        Authority::Machine,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/routines/notices/ack",
        Operation::RoutineNoticeAck,
        "routine-notice-ack",
        Group::Routines,
        Authority::Machine,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/routines",
        Operation::RoutineList,
        "routine-list",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/routines/:routine_id/revisions/:revision/steps/:offset",
        Operation::RoutineSteps,
        "routine-steps",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/teams/:team_id/routines/:routine_id",
        Operation::RoutineDelete,
        "routine-delete",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/proposals/:proposal_id",
        Operation::RoutineProposalConfirm,
        "routine-proposal-confirm",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/teams/:team_id/routines/proposals/:proposal_id",
        Operation::RoutineProposalRevoke,
        "routine-proposal-revoke",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/runs/:run_id/segment",
        Operation::RoutineRun,
        "routine-run",
        Group::Routines,
        Authority::Routine,
        Reply::Ndjson,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/runs/:run_id/challenge",
        Operation::RoutineChallengeOpen,
        "routine-challenge-open",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/runs/:run_id/human",
        Operation::RoutineHumanSubmit,
        "routine-human-submit",
        Group::Routines,
        Authority::Supervisor,
        Reply::Ndjson,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/runs/:run_id/integrations",
        Operation::RoutineIntegrationSubmit,
        "routine-integration-submit",
        Group::Routines,
        Authority::Supervisor,
        Reply::Ndjson,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/incidents/:run_id/card",
        Operation::RoutineCardOpen,
        "routine-card-open",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/incidents/:run_id/answer",
        Operation::RoutineCardAnswer,
        "routine-card-answer",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/:routine_id/resume",
        Operation::RoutineResume,
        "routine-resume",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/routines/:routine_id/pause",
        Operation::RoutinePause,
        "routine-pause",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/routines/runs/:run_id/diagnostics",
        Operation::RoutineDiagnostics,
        "routine-diagnostics",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/routines/runs/:run_id/steps/:snapshot/:offset",
        Operation::RoutineRunSteps,
        "routine-run-steps",
        Group::Routines,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/healthz",
        Operation::Health,
        "health",
        Group::Controller,
        Authority::Machine,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/activity",
        Operation::Activity,
        "activity",
        Group::Controller,
        Authority::Machine,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/local-assistants",
        Operation::LocalAssistantList,
        "local-assistant-list",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/local-assistants/:image_hash/icon",
        Operation::LocalAssistantIcon,
        "local-assistant-icon",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Binary,
    ),
    route(
        Method::Get,
        "/v1/local-assistants/:image_hash/summary/:locale",
        Operation::LocalAssistantSummary,
        "local-assistant-summary",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/local-assistants/:image_hash/details/:locale",
        Operation::LocalAssistantDetails,
        "local-assistant-details",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/assistants",
        Operation::RegistryList,
        "registry-list",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/space/bootstrap",
        Operation::SpaceBootstrapReset,
        "space-bootstrap-reset",
        Group::Teams,
        Authority::Machine,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/space",
        Operation::SpaceReset,
        "space-reset",
        Group::Teams,
        Authority::SupervisorOrHostReset,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/space/supervisor-key",
        Operation::SupervisorKeyRotate,
        "supervisor-key-rotate",
        Group::Controller,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/assistants",
        Operation::AssistantList,
        "assistant-list",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/assistant-registry",
        Operation::AssistantRegistry,
        "assistant-registry",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/assistants/:assistant_id/icon",
        Operation::AssistantIcon,
        "assistant-icon",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Binary,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/assistants/:assistant_id/summary/:locale",
        Operation::AssistantSummary,
        "assistant-summary",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Get,
        "/v1/teams/:team_id/assistants/:assistant_id/details/:locale",
        Operation::AssistantDetails,
        "assistant-details",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/assistants/local/fresh",
        Operation::LocalAssistantFreshInstall,
        "local-assistant-fresh-install",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/assistants",
        Operation::AssistantInstall,
        "assistant-install",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/assistants/local",
        Operation::LocalAssistantInstall,
        "local-assistant-install",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Delete,
        "/v1/teams/:team_id/assistants/:assistant_id",
        Operation::AssistantUninstall,
        "assistant-uninstall",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
    route(
        Method::Post,
        "/v1/teams/:team_id/assistants/:assistant_id/actions/:action_id",
        Operation::AssistantInvoke,
        "assistant-invoke",
        Group::Assistants,
        Authority::Supervisor,
        Reply::Json,
    ),
];

impl Operation {
    /// This operation's route.
    #[must_use]
    pub const fn route(self) -> &'static Route {
        &ROUTES[self as usize]
    }

    /// The operation's wire and audit name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        self.route().name
    }
}

/// A resolved request: its operation and the captured path segments, by name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RouteMatch<'a> {
    /// The operation.
    pub operation: Operation,
    /// Each `:name` segment's value, in pattern order.
    pub params: Vec<(&'static str, &'a str)>,
}

impl RouteMatch<'_> {
    /// The value captured for `name`.
    #[must_use]
    pub fn param(&self, name: &str) -> Option<&str> {
        self.params
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| *value)
    }
}

/// The first route matching `method` and `path`, or `None` (a 404).
#[must_use]
pub fn resolve(method: Method, path: &str) -> Option<RouteMatch<'_>> {
    let parts: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
    ROUTES
        .iter()
        .filter(|route| route.method == method)
        .find_map(|route| matched(route, &parts))
}

fn matched<'a>(route: &Route, parts: &[&'a str]) -> Option<RouteMatch<'a>> {
    let pattern: Vec<&'static str> = route
        .pattern
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    if pattern.len() != parts.len() {
        return None;
    }
    let mut params = Vec::new();
    for (expected, actual) in pattern.into_iter().zip(parts) {
        if let Some(name) = expected.strip_prefix(':') {
            params.push((name, *actual));
        } else if expected != *actual {
            return None;
        }
    }
    Some(RouteMatch {
        operation: route.operation,
        params,
    })
}

#[cfg(test)]
mod tests;
