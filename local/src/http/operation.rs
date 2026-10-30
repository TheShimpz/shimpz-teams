//! The closed set of the controller's 77 operations, in the route table's order (generated once from
//! `core/http/strict.py`'s `CONTROLLER_ROUTES`; the table in [`super::route`] is now the authority).

/// One controller operation, named on the wire and in the audit log by [`Operation::name`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Operation {
    /// `team-list`: `GET /v1/teams`.
    TeamList = 0,
    /// `assistant-integration-complete`: `POST /v1/oauth/cloudflare/callback`.
    AssistantIntegrationComplete = 1,
    /// `team-create`: `POST /v1/teams/:team_id/create`.
    TeamCreate = 2,
    /// `team-destroy`: `DELETE /v1/teams/:team_id`.
    TeamDestroy = 3,
    /// `team-rename`: `PATCH /v1/teams/:team_id`.
    TeamRename = 4,
    /// `file-list`: `GET /v1/teams/:team_id/files`.
    FileList = 5,
    /// `file-upload`: `POST /v1/teams/:team_id/files`.
    FileUpload = 6,
    /// `file-preview`: `GET /v1/teams/:team_id/files/:file_id/preview`.
    FilePreview = 7,
    /// `file-page-preview`: `GET /v1/teams/:team_id/files/:file_id/pages/:page/preview`.
    FilePagePreview = 8,
    /// `file-content`: `GET /v1/teams/:team_id/files/:file_id/content`.
    FileContent = 9,
    /// `file-acknowledgement`: `POST /v1/teams/:team_id/files/:file_id/acknowledgement`.
    FileAcknowledgement = 10,
    /// `browser-view-open`: `POST /v1/teams/:team_id/browser/view`.
    BrowserViewOpen = 11,
    /// `browser-view-stream`: `GET /v1/teams/:team_id/browser/views/:view_id/stream`.
    BrowserViewStream = 12,
    /// `browser-view-control`: `GET /v1/teams/:team_id/browser/views/:view_id/control`.
    BrowserViewControl = 13,
    /// `browser-sign-in-start`: `POST /v1/teams/:team_id/browser/sign-in`.
    BrowserSignInStart = 14,
    /// `browser-sign-in-save`: `POST /v1/teams/:team_id/browser/sign-in/save`.
    BrowserSignInSave = 15,
    /// `browser-sign-in-cancel`: `POST /v1/teams/:team_id/browser/sign-in/cancel`.
    BrowserSignInCancel = 16,
    /// `browser-sign-in`: `GET /v1/teams/:team_id/browser/sign-in`.
    BrowserSignIn = 17,
    /// `browser-sign-in-delete`: `POST /v1/teams/:team_id/browser/sign-in/delete`.
    BrowserSignInDelete = 18,
    /// `inference-status`: `GET /v1/teams/:team_id/inference`.
    InferenceStatus = 19,
    /// `inference-configure`: `PUT /v1/teams/:team_id/inference`.
    InferenceConfigure = 20,
    /// `chat`: `POST /v1/teams/:team_id/chat`.
    Chat = 21,
    /// `chat-capability-plan`: `POST /v1/teams/:team_id/chat/capability-plan`.
    ChatCapabilityPlan = 22,
    /// `chat-intent-route`: `POST /v1/teams/:team_id/chat/intent-route`.
    ChatIntentRoute = 23,
    /// `chat-integration-pending`: `GET /v1/teams/:team_id/chat/integrations`.
    ChatIntegrationPending = 24,
    /// `chat-integration-submit`: `POST /v1/teams/:team_id/chat/integrations`.
    ChatIntegrationSubmit = 25,
    /// `chat-human-pending`: `GET /v1/teams/:team_id/chat/human`.
    ChatHumanPending = 26,
    /// `chat-human-submit`: `POST /v1/teams/:team_id/chat/human`.
    ChatHumanSubmit = 27,
    /// `chat-human-open`: `POST /v1/teams/:team_id/chat/human/challenge`.
    ChatHumanOpen = 28,
    /// `chat-stop`: `POST /v1/teams/:team_id/chat/stop`.
    ChatStop = 29,
    /// `chat-limit-read`: `GET /v1/chat/limit`.
    ChatLimitRead = 30,
    /// `chat-limit-save`: `PUT /v1/chat/limit`.
    ChatLimitSave = 31,
    /// `assistant-integration-list`: `GET /v1/teams/:team_id/assistant-integrations`.
    AssistantIntegrationList = 32,
    /// `assistant-integration-authorize`: `POST /v1/teams/:team_id/assistant-integrations/challenges/:challenge_id/authorize`.
    AssistantIntegrationAuthorize = 33,
    /// `assistant-integration-cancel`: `DELETE /v1/teams/:team_id/assistant-integrations/challenges/:challenge_id/authorize`.
    AssistantIntegrationCancel = 34,
    /// `assistant-stored-input-list`: `GET /v1/teams/:team_id/assistant-stored-inputs`.
    AssistantStoredInputList = 35,
    /// `assistant-stored-input-clear`: `DELETE /v1/teams/:team_id/assistant-stored-inputs/:assistant_id/:stored_input_id`.
    AssistantStoredInputClear = 36,
    /// `routine-claim`: `POST /v1/routines/claim`.
    RoutineClaim = 37,
    /// `routine-notices`: `GET /v1/routines/notices`.
    RoutineNotices = 38,
    /// `routine-notice-ack`: `POST /v1/routines/notices/ack`.
    RoutineNoticeAck = 39,
    /// `routine-list`: `GET /v1/teams/:team_id/routines`.
    RoutineList = 40,
    /// `routine-steps`: `GET /v1/teams/:team_id/routines/:routine_id/revisions/:revision/steps/:offset`.
    RoutineSteps = 41,
    /// `routine-delete`: `DELETE /v1/teams/:team_id/routines/:routine_id`.
    RoutineDelete = 42,
    /// `routine-proposal-confirm`: `POST /v1/teams/:team_id/routines/proposals/:proposal_id`.
    RoutineProposalConfirm = 43,
    /// `routine-proposal-revoke`: `DELETE /v1/teams/:team_id/routines/proposals/:proposal_id`.
    RoutineProposalRevoke = 44,
    /// `routine-run`: `POST /v1/teams/:team_id/routines/runs/:run_id/segment`.
    RoutineRun = 45,
    /// `routine-challenge-open`: `POST /v1/teams/:team_id/routines/runs/:run_id/challenge`.
    RoutineChallengeOpen = 46,
    /// `routine-human-submit`: `POST /v1/teams/:team_id/routines/runs/:run_id/human`.
    RoutineHumanSubmit = 47,
    /// `routine-integration-submit`: `POST /v1/teams/:team_id/routines/runs/:run_id/integrations`.
    RoutineIntegrationSubmit = 48,
    /// `routine-card-open`: `POST /v1/teams/:team_id/routines/incidents/:run_id/card`.
    RoutineCardOpen = 49,
    /// `routine-card-answer`: `POST /v1/teams/:team_id/routines/incidents/:run_id/answer`.
    RoutineCardAnswer = 50,
    /// `routine-resume`: `POST /v1/teams/:team_id/routines/:routine_id/resume`.
    RoutineResume = 51,
    /// `routine-pause`: `POST /v1/teams/:team_id/routines/:routine_id/pause`.
    RoutinePause = 52,
    /// `routine-diagnostics`: `GET /v1/teams/:team_id/routines/runs/:run_id/diagnostics`.
    RoutineDiagnostics = 53,
    /// `routine-run-steps`: `GET /v1/teams/:team_id/routines/runs/:run_id/steps/:snapshot/:offset`.
    RoutineRunSteps = 54,
    /// `health`: `GET /healthz`.
    Health = 55,
    /// `activity`: `GET /v1/activity`.
    Activity = 56,
    /// `local-assistant-list`: `GET /v1/local-assistants`.
    LocalAssistantList = 57,
    /// `local-assistant-icon`: `GET /v1/local-assistants/:image_hash/icon`.
    LocalAssistantIcon = 58,
    /// `local-assistant-summary`: `GET /v1/local-assistants/:image_hash/summary/:locale`.
    LocalAssistantSummary = 59,
    /// `local-assistant-details`: `GET /v1/local-assistants/:image_hash/details/:locale`.
    LocalAssistantDetails = 60,
    /// `registry-list`: `GET /v1/assistants`.
    RegistryList = 61,
    /// `space-bootstrap-reset`: `DELETE /v1/space/bootstrap`.
    SpaceBootstrapReset = 62,
    /// `space-reset`: `DELETE /v1/space`.
    SpaceReset = 63,
    /// `supervisor-key-rotate`: `POST /v1/space/supervisor-key`.
    SupervisorKeyRotate = 64,
    /// `assistant-list`: `GET /v1/teams/:team_id/assistants`.
    AssistantList = 65,
    /// `assistant-registry`: `GET /v1/teams/:team_id/assistant-registry`.
    AssistantRegistry = 66,
    /// `assistant-icon`: `GET /v1/teams/:team_id/assistants/:assistant_id/icon`.
    AssistantIcon = 67,
    /// `assistant-summary`: `GET /v1/teams/:team_id/assistants/:assistant_id/summary/:locale`.
    AssistantSummary = 68,
    /// `assistant-details`: `GET /v1/teams/:team_id/assistants/:assistant_id/details/:locale`.
    AssistantDetails = 69,
    /// `local-assistant-fresh-install`: `POST /v1/teams/:team_id/assistants/local/fresh`.
    LocalAssistantFreshInstall = 70,
    /// `assistant-install`: `POST /v1/teams/:team_id/assistants`.
    AssistantInstall = 71,
    /// `local-assistant-install`: `POST /v1/teams/:team_id/assistants/local`.
    LocalAssistantInstall = 72,
    /// `assistant-uninstall`: `DELETE /v1/teams/:team_id/assistants/:assistant_id`.
    AssistantUninstall = 73,
    /// `assistant-invoke`: `POST /v1/teams/:team_id/assistants/:assistant_id/actions/:action_id`.
    AssistantInvoke = 74,
    /// `chat-delivered`: `POST /v1/teams/:team_id/chat/delivered`.
    ChatDelivered = 75,
    /// `chat-revoked`: `POST /v1/teams/:team_id/chat/revoked`.
    ChatRevoked = 76,
}
