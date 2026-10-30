//! The seam between the HTTP layer and the services that answer operations (frozen in W0).

use std::borrow::Cow;
use std::sync::Arc;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_inference::brain::ModelCredential;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::mpsc;

use super::operation::Operation;
use super::route::{Group, RouteMatch};

/// Who the HTTP layer proved the caller to be.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Principal {
    /// The controller bearer alone.
    Machine,
    /// A Supervisor session assertion: who signed it, and the human assurance it bound, if any.
    Supervisor {
        /// The Supervisor's opaque 32-hex id (the assertion's subject).
        supervisor_id: String,
        /// The assurance kind it bound (`auth:password`, `auth:totp`, or `auth:passkey`), only on an approval of an
        /// authentication challenge.
        assurance: Option<String>,
        /// The SHA-256 of the session it was admitted under.
        authority_sha256: String,
    },
    /// The CLI's host-reset assertion (only on `space-reset`).
    HostReset,
    /// A Routine assertion; the service then checks the run's lease against it.
    Routine {
        /// The SHA-256 of the Routine public key that signed it.
        key_fingerprint: String,
        /// The lease digest the assertion bound.
        lease_sha256: String,
    },
}

/// A request body the HTTP layer captured within the operation's bound.
#[derive(Clone, Debug, PartialEq)]
pub enum Body {
    /// No body.
    None,
    /// One strict JSON document.
    Json(Value),
    /// One file upload's metadata and content.
    File(FileUpload),
}

/// One uploaded file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileUpload {
    /// Its validated file name.
    pub filename: String,
    /// Its declared media type.
    pub media_type: String,
    /// Its content (at most 25 MiB).
    pub content: Vec<u8>,
}

/// One authorized operation call.
#[derive(Debug)]
pub struct Call<'a> {
    /// The operation and its captured path segments.
    pub route: &'a RouteMatch<'a>,
    /// The proved caller.
    pub principal: Principal,
    /// The captured body.
    pub body: Body,
    /// The request-only model credential, on model-bound operations.
    pub model: Option<ModelCredential>,
    /// The request-only decision (intent classification) key, when bound.
    pub decision_key: Option<Secret>,
    /// The metadata-only trace id the answer carries.
    pub trace_id: String,
    /// On a Browser view upgrade, the viewer's admitted WebSocket key the display must answer.
    pub upgrade_key: Option<String>,
}

impl Call<'_> {
    /// The operation called.
    #[must_use]
    pub fn operation(&self) -> Operation {
        self.route.operation
    }
}

/// One record of an NDJSON answer.
#[derive(Clone, Debug, PartialEq)]
pub enum StreamRecord {
    /// A metadata-only progress record.
    Progress(Value),
    /// The one terminal record: the operation's status and answer.
    Terminal {
        /// The HTTP status the operation would have answered.
        status: u16,
        /// Its answer object.
        body: Value,
    },
}

/// The client side of an upgraded connection.
pub trait Upgraded: AsyncRead + AsyncWrite + Send + Unpin {}

impl<T: AsyncRead + AsyncWrite + Send + Unpin> Upgraded for T {}

/// A relay whose far end already answered the upgrade; the HTTP layer answers `101` and then runs it.
pub trait Relay: Send {
    /// Copies bytes both ways between `client` and the far end until either closes.
    fn run(self: Box<Self>, client: Box<dyn Upgraded>) -> BoxFuture<'static, ()>;
}

/// A successful answer, of the kind the route declares.
pub enum Answer {
    /// One JSON document and its status.
    Json {
        /// The HTTP status.
        status: u16,
        /// The document; the HTTP layer adds `trace_id` and enforces the response cap.
        body: Value,
    },
    /// Progress records then one terminal record. The producer ignores a closed receiver: a disconnected consumer
    /// never changes the admitted operation's outcome.
    Stream(mpsc::Receiver<StreamRecord>),
    /// Bytes of one media type.
    Binary {
        /// The `Content-Type`.
        media_type: &'static str,
        /// The bytes.
        body: Vec<u8>,
        /// Further headers (`Repr-Digest`, the page count).
        headers: Vec<(&'static str, String)>,
    },
    /// An upgrade the far end already accepted.
    Relay(Box<dyn Relay>),
}

impl std::fmt::Debug for Answer {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Json { status, .. } => formatter
                .debug_struct("Json")
                .field("status", status)
                .finish_non_exhaustive(),
            Self::Stream(_) => formatter.write_str("Stream"),
            Self::Binary { media_type, .. } => formatter
                .debug_struct("Binary")
                .field("media_type", media_type)
                .finish_non_exhaustive(),
            Self::Relay(_) => formatter.write_str("Relay"),
        }
    }
}

/// A refused or failed operation, projected without any upstream text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Problem {
    /// The HTTP status.
    pub status: u16,
    /// The fixed public message.
    pub message: Cow<'static, str>,
    /// The stable code, when the operation has one.
    pub code: Option<&'static str>,
    /// For a busy answer, how long the caller waits before retrying.
    pub retry_after_ms: Option<u32>,
}

impl Problem {
    /// A problem with a fixed message and code.
    #[must_use]
    pub const fn new(status: u16, message: &'static str, code: &'static str) -> Self {
        Self {
            status,
            message: Cow::Borrowed(message),
            code: Some(code),
            retry_after_ms: None,
        }
    }
}

/// One service group.
pub trait Service: Send + Sync {
    /// Answers one authorized call of an operation of this group.
    ///
    /// # Errors
    /// [`Problem`] for every refusal or failure.
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>>;
}

/// Every service group, as the composition root wires them.
#[derive(Clone)]
pub struct Services {
    /// Health, activity, and the Supervisor key rotation.
    pub controller: Arc<dyn Service>,
    /// Team lifecycle and the Space reset.
    pub teams: Arc<dyn Service>,
    /// Team files.
    pub files: Arc<dyn Service>,
    /// Browser views and Sign-ins.
    pub browser: Arc<dyn Service>,
    /// Inference settings.
    pub inference: Arc<dyn Service>,
    /// Chat.
    pub chat: Arc<dyn Service>,
    /// Integrations and Stored Inputs.
    pub integrations: Arc<dyn Service>,
    /// Routines.
    pub routines: Arc<dyn Service>,
    /// Assistants.
    pub assistants: Arc<dyn Service>,
}

impl Services {
    /// The service that answers `group`.
    #[must_use]
    pub fn group(&self, group: Group) -> &dyn Service {
        match group {
            Group::Controller => self.controller.as_ref(),
            Group::Teams => self.teams.as_ref(),
            Group::Files => self.files.as_ref(),
            Group::Browser => self.browser.as_ref(),
            Group::Inference => self.inference.as_ref(),
            Group::Chat => self.chat.as_ref(),
            Group::Integrations => self.integrations.as_ref(),
            Group::Routines => self.routines.as_ref(),
            Group::Assistants => self.assistants.as_ref(),
        }
    }
}

impl std::fmt::Debug for Services {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Services")
    }
}
