//! Every bound the server enforces: admission, deadlines, request bodies by operation, and response sizes.

use std::time::Duration;

use team_protocol::payload::MAX_TEAM_REGISTRY_BYTES;
use team_protocol::routine_notice::MAX_ROUTINE_LIST_BYTES;
use team_protocol::turn::{MAX_CAPABILITY_PLAN_BODY_BYTES, MAX_INTENT_ROUTE_BODY_BYTES};

use crate::http::operation::Operation;

/// The bound of an ordinary JSON request body.
pub const MAX_BODY_BYTES: usize = 16 * 1024;
/// The bound of one answer to a pending human request, from a chat turn or a Routine run alike.
pub const MAX_HUMAN_RESPONSE_BODY_BYTES: usize = 128 * 1024;
/// The bound of a chat turn's body.
pub const MAX_CHAT_BODY_BYTES: usize = 128 * 1024;
/// The bound of a Routine notice acknowledgement.
pub const MAX_NOTICE_ACK_BODY_BYTES: usize = 64 * 1024;
/// The bound of one JSON answer, unless its operation has its own allowance.
pub const MAX_API_RESPONSE_BYTES: usize = 128 * 1024;
/// The bound of one uploaded file (the protocol's `MAX_FILE_UPLOAD_BYTES`).
pub const MAX_FILE_BYTES: usize = 25 * 1024 * 1024;
/// The longest request target.
pub const MAX_REQUEST_TARGET_BYTES: usize = 512;
/// The most header fields one request may carry.
pub const MAX_HEADERS: usize = 100;
/// The largest request head.
pub const MAX_HEAD_BYTES: usize = 64 * 1024;
/// The listen backlog.
pub const BACKLOG: u32 = 32;
/// How long a busy caller waits before retrying.
pub const RETRY_AFTER_MS: u32 = 250;

/// The server's admission and time bounds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Connections served at once; one more is closed unanswered.
    pub slots: usize,
    /// The absolute bound of a request head, from the connection's start.
    pub header_deadline: Duration,
    /// The absolute bound of a request body, from its head's end.
    pub body_deadline: Duration,
    /// The longest one write may wait for the peer to read.
    pub write_idle: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            slots: 16,
            header_deadline: Duration::from_secs(10),
            body_deadline: Duration::from_secs(10),
            write_idle: Duration::from_secs(10),
        }
    }
}

/// What body an operation admits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BodyKind {
    /// None: a declared length must be zero.
    None,
    /// One JSON object of at most this many bytes.
    Json(usize),
    /// One file of 1 to [`MAX_FILE_BYTES`] bytes, read only once the request is authorized.
    File,
}

/// The body `operation` admits.
#[must_use]
pub const fn body(operation: Operation) -> BodyKind {
    use Operation as O;
    match operation {
        O::FileUpload => BodyKind::File,
        O::Chat => BodyKind::Json(MAX_CHAT_BODY_BYTES),
        O::ChatCapabilityPlan => BodyKind::Json(MAX_CAPABILITY_PLAN_BODY_BYTES),
        O::ChatIntentRoute => BodyKind::Json(MAX_INTENT_ROUTE_BODY_BYTES),
        O::ChatHumanSubmit | O::RoutineHumanSubmit => BodyKind::Json(MAX_HUMAN_RESPONSE_BODY_BYTES),
        O::RoutineNoticeAck => BodyKind::Json(MAX_NOTICE_ACK_BODY_BYTES),
        O::SupervisorKeyRotate
        | O::AssistantInstall
        | O::LocalAssistantFreshInstall
        | O::LocalAssistantInstall
        | O::AssistantIntegrationAuthorize
        | O::AssistantIntegrationCancel
        | O::AssistantIntegrationComplete
        | O::AssistantInvoke
        | O::InferenceConfigure
        | O::TeamCreate
        | O::TeamDestroy
        | O::TeamRename
        | O::ChatIntegrationSubmit
        | O::ChatHumanOpen
        | O::ChatStop
        | O::ChatDelivered
        | O::ChatRevoked
        | O::ChatLimitSave
        | O::BrowserSignInStart
        | O::BrowserSignInSave
        | O::BrowserSignInCancel
        | O::BrowserSignInDelete
        | O::RoutineClaim
        | O::RoutineRun
        | O::RoutineChallengeOpen
        | O::RoutineIntegrationSubmit
        | O::RoutineCardOpen
        | O::RoutineCardAnswer
        | O::RoutineResume
        | O::RoutinePause
        | O::RoutineProposalConfirm => BodyKind::Json(MAX_BODY_BYTES),
        _ => BodyKind::None,
    }
}

/// The bound of `operation`'s JSON answer.
#[must_use]
pub const fn response(operation: Operation) -> usize {
    match operation {
        Operation::RoutineList => MAX_ROUTINE_LIST_BYTES,
        Operation::AssistantRegistry => MAX_TEAM_REGISTRY_BYTES,
        _ => MAX_API_RESPONSE_BYTES,
    }
}

/// Whether `operation` carries the Team's model credential, whose digest its assertion must bind.
#[must_use]
pub const fn model_bound(operation: Operation) -> bool {
    matches!(
        operation,
        Operation::Chat
            | Operation::ChatCapabilityPlan
            | Operation::ChatIntentRoute
            | Operation::ChatHumanSubmit
            | Operation::ChatIntegrationSubmit
    )
}

/// Whether the audit journal never blocks `operation` (ADR-0120): a Sign-in Cancel and deletion protect the Team,
/// and a Save's committed answer must still reach the Supervisor.
#[must_use]
pub const fn journal_tolerant(operation: Operation) -> bool {
    matches!(
        operation,
        Operation::BrowserSignInSave
            | Operation::BrowserSignInCancel
            | Operation::BrowserSignInDelete
    )
}

#[cfg(test)]
mod tests {
    use super::{BodyKind, MAX_BODY_BYTES, MAX_FILE_BYTES, body, response};
    use crate::http::route::ROUTES;

    #[test]
    fn every_body_bound_is_one_the_protocol_names() {
        let mut json = 0;
        for route in &ROUTES {
            match body(route.operation) {
                BodyKind::Json(limit) => {
                    json += 1;
                    assert!(
                        [16, 64, 128, 192, 256].contains(&(limit / 1024)),
                        "{}",
                        route.name
                    );
                }
                BodyKind::File => assert_eq!(route.name, "file-upload"),
                BodyKind::None => {}
            }
            assert!(response(route.operation) >= 128 * 1024);
        }
        assert_eq!(json, 37);
        assert_eq!(
            i64::try_from(MAX_FILE_BYTES),
            Ok(team_protocol::payload::MAX_FILE_UPLOAD_BYTES)
        );
        assert_eq!(
            body(crate::http::operation::Operation::RoutineRun),
            BodyKind::Json(MAX_BODY_BYTES)
        );
    }
}
