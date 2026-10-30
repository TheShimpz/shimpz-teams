//! The Files service group (`Group::Files`): a Team's file listing and upload, a listed file's sanitized preview (an
//! image's, or one PDF page's with its page count), the verified original's download, and the Supervisor's
//! acknowledgement of a file from the web (ADR-0093, ADR-0117 §7).
//!
//! A listing, an upload, and an acknowledgement hold the Team's stripe, so a teardown or reset never interleaves with
//! them. A preview or download holds no lock: it is registered in the preview book, which a Team's teardown and the
//! Space reset close and drain first; a preview never renews a file's retention and is answered only while the file is
//! still listed. At most two downloads run at once; a download's release is audited by file id and size before its
//! first byte leaves, and a file from the web leaves only once the requesting Supervisor acknowledged it.

use std::sync::Arc;

use base64::Engine as _;
use strict_json::{Number, Value};
use team_core::audit::AuditSink;
use team_core::future::BoxFuture;
use team_protocol::preview::{PAGE_COUNT_HEADER, canonical_page, canonical_page_framing};
use team_protocol::{content, preview};
use tokio::sync::Semaphore;

use super::Rendition;
use super::service::{LocalPreparer, RenditionError};
use super::store::{Store, TeamPresence, Unacknowledged};
use crate::coordination::Coordination;
use crate::http::operation::Operation;
use crate::http::server::{RequestContext, audit_event, current_request};
use crate::http::service::{Answer, Body, Call, Problem, Service};
use team_storage::files::{StorageError, StorageErrorKind};

/// The most downloads that run at once.
pub const MAX_DOWNLOADS: usize = 2;
const RETRY_AFTER_MS: u32 = 250;

/// The Files service.
pub struct Files {
    /// Previews.
    pub preparer: Arc<LocalPreparer>,
    /// The Team files store.
    pub store: Store,
    /// Whether a Team exists.
    pub presence: Arc<dyn TeamPresence>,
    /// The Team stripes and the preview book.
    pub coordination: Coordination,
    /// The audit journal a release and an acknowledgement are recorded in.
    pub audit: Arc<dyn AuditSink>,
    downloads: Semaphore,
}

impl std::fmt::Debug for Files {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Files").finish_non_exhaustive()
    }
}

fn not_found() -> Problem {
    Problem::new(404, "file not found", "file-not-found")
}

fn busy(message: &'static str, code: &'static str) -> Problem {
    Problem {
        retry_after_ms: Some(RETRY_AFTER_MS),
        ..Problem::new(503, message, code)
    }
}

fn internal(message: &'static str, code: &'static str) -> Problem {
    Problem::new(500, message, code)
}

/// The public problem of a storage failure.
#[must_use]
pub fn storage_problem(error: &StorageError) -> Problem {
    match error.kind {
        StorageErrorKind::NotFound => not_found(),
        StorageErrorKind::Quota => Problem::new(507, error.message, "storage-quota-exceeded"),
        StorageErrorKind::Input => Problem::new(422, error.message, "invalid-file"),
        StorageErrorKind::NotWeb => Problem::new(
            409,
            "the file did not come from the web",
            "file-not-from-web",
        ),
        StorageErrorKind::Unsafe => internal(
            "Team storage failed its safety checks",
            "storage-safety-failed",
        ),
    }
}

fn acknowledgement_required() -> Problem {
    Problem::new(
        403,
        "the file came from the web and needs an acknowledgement",
        "file-acknowledgement-required",
    )
}

fn json(body: Vec<(&str, Value)>) -> Answer {
    Answer::Json {
        status: 200,
        body: Value::Object(
            body.into_iter()
                .map(|(name, value)| (name.to_owned(), value))
                .collect(),
        ),
    }
}

/// The verified human principal of the request, or `None` for a machine or nobody.
fn human_principal(context: Option<&RequestContext>) -> Option<String> {
    context
        .filter(|context| context.principal_class == "human")
        .and_then(|context| context.principal_id.clone())
}

impl Files {
    /// The Files service over its seams.
    #[must_use]
    pub fn new(
        preparer: Arc<LocalPreparer>,
        store: Store,
        presence: Arc<dyn TeamPresence>,
        coordination: Coordination,
        audit: Arc<dyn AuditSink>,
    ) -> Self {
        Self {
            preparer,
            store,
            presence,
            coordination,
            audit,
            downloads: Semaphore::new(MAX_DOWNLOADS),
        }
    }

    async fn list(&self, team_id: &str) -> Result<Answer, Problem> {
        let _stripe = self.coordination.locks.team(team_id).await;
        self.presence.require(team_id).await?;
        let listing = self
            .store
            .list(team_id)
            .await
            .map_err(|error| storage_problem(&error))?;
        let Value::Object(members) = listing.to_value() else {
            return Err(internal(
                "Team storage failed its safety checks",
                "storage-safety-failed",
            ));
        };
        let mut body = vec![("team_id".to_owned(), Value::String(team_id.to_owned()))];
        body.extend(members.into_iter().filter(|(name, _)| name != "team_id"));
        Ok(Answer::Json {
            status: 200,
            body: Value::Object(body),
        })
    }

    async fn upload(&self, team_id: &str, body: &Body) -> Result<Answer, Problem> {
        let Body::File(upload) = body else {
            return Err(Problem::new(422, "file must contain bytes", "invalid-file"));
        };
        let _stripe = self.coordination.locks.team(team_id).await;
        self.presence.require(team_id).await?;
        let stored = self
            .store
            .put(team_id, upload)
            .await
            .map_err(|error| storage_problem(&error))?;
        Ok(json(vec![
            ("team_id", Value::String(team_id.to_owned())),
            ("file", stored.to_value()),
        ]))
    }

    async fn preview(
        &self,
        team_id: &str,
        file_id: &str,
        page: Option<&str>,
    ) -> Result<Answer, Problem> {
        let page = match page {
            None => None,
            Some(page) => Some(
                canonical_page(&Value::String(page.to_owned()))
                    .ok_or_else(|| Problem::new(404, "page not found", "file-not-found"))?,
            ),
        };
        let open = self
            .coordination
            .previews
            .open(team_id)
            .map_err(|_| not_found())?;
        self.presence.require(team_id).await?;
        let item = self
            .store
            .listed(team_id, file_id)
            .await
            .map_err(|error| storage_problem(&error))?;
        let rendered = self
            .preparer
            .render(team_id, &item, page, &open)
            .await
            .map_err(|error| match error {
                RenditionError::Ended => not_found(),
                RenditionError::Busy => busy("file preview capacity is busy", "file-preview-busy"),
                RenditionError::Unavailable => Problem::new(
                    503,
                    "the file preview could not be prepared",
                    "file-preview-unavailable",
                ),
                RenditionError::Store(error) => storage_problem(&error),
            })?;
        // A sweep may have collected the file while it was prepared: only a file still listed is answered.
        self.store
            .listed(team_id, file_id)
            .await
            .map_err(|error| storage_problem(&error))?;
        let rendition = rendered.ok_or_else(|| {
            Problem::new(422, "the file has no preview", "file-preview-unsupported")
        })?;
        framed(rendition, page.is_some())
    }

    async fn content(
        &self,
        team_id: &str,
        file_id: &str,
        context: Option<RequestContext>,
    ) -> Result<Answer, Problem> {
        let _slot = self
            .downloads
            .try_acquire()
            .map_err(|_| busy("file download capacity is busy", "file-download-busy"))?;
        let reading = self
            .coordination
            .previews
            .open(team_id)
            .map_err(|_| not_found())?;
        self.presence.require(team_id).await?;
        self.store
            .listed(team_id, file_id)
            .await
            .map_err(|error| storage_problem(&error))?;
        let (stored, original) = self
            .store
            .read(team_id, file_id)
            .await
            .map_err(|error| storage_problem(&error))?;
        let item = stored.metadata;
        let principal = human_principal(context.as_ref());
        let released = self
            .store
            .released(team_id, &item.id, principal.as_deref())
            .await
            .map_err(|error| storage_problem(&error))?;
        if !released {
            return Err(acknowledgement_required());
        }
        reading.check().map_err(|_| not_found())?;
        self.release(team_id, &item.id, &item.sha256, original, context.as_ref())
            .await
    }

    /// The original framed by its exact length and SHA-256, its release audited by file id and size first.
    async fn release(
        &self,
        team_id: &str,
        file_id: &str,
        sha256: &str,
        original: Vec<u8>,
        context: Option<&RequestContext>,
    ) -> Result<Answer, Problem> {
        let length = i64::try_from(original.len()).unwrap_or(i64::MAX);
        let framing = Value::Object(vec![
            ("length".to_owned(), Value::Number(Number::Int(length))),
            ("sha256".to_owned(), Value::String(sha256.to_owned())),
        ]);
        let digest = hex_bytes(sha256)
            .filter(|_| content::canonical_framing(&framing).is_some())
            .ok_or_else(|| {
                internal(
                    "the file content failed its framing",
                    "file-content-invalid",
                )
            })?;
        let detail = format!("file={file_id} bytes={}", original.len());
        self.record(context, "file-content", team_id, &detail)
            .await?;
        let digest = base64::engine::general_purpose::STANDARD.encode(digest);
        Ok(Answer::Binary {
            media_type: "application/octet-stream",
            body: original,
            headers: vec![("Repr-Digest", format!("sha-256=:{digest}:"))],
        })
    }

    async fn acknowledge(
        &self,
        team_id: &str,
        file_id: &str,
        context: Option<RequestContext>,
    ) -> Result<Answer, Problem> {
        let principal = human_principal(context.as_ref()).ok_or_else(acknowledgement_required)?;
        let context = context.ok_or_else(acknowledgement_required)?;
        let _stripe = self.coordination.locks.team(team_id).await;
        self.presence.require(team_id).await?;
        let event = audit_event(&context, "file-acknowledgement", "ok")
            .with("team_id", Value::String(team_id.to_owned()))
            .with("detail", Value::String(format!("file={file_id}")));
        self.store
            .acknowledge(team_id, file_id, &principal, Arc::clone(&self.audit), event)
            .await
            .map_err(|error| match error {
                Unacknowledged::Storage(error) => storage_problem(&error),
                Unacknowledged::Unrecorded => internal("internal error", "internal-error"),
            })?;
        Ok(json(vec![
            ("file_id", Value::String(file_id.to_owned())),
            ("acknowledged", Value::Bool(true)),
        ]))
    }

    async fn record(
        &self,
        context: Option<&RequestContext>,
        operation: &str,
        team_id: &str,
        detail: &str,
    ) -> Result<(), Problem> {
        let context = context.ok_or_else(|| internal("internal error", "internal-error"))?;
        let event = audit_event(context, operation, "ok")
            .with("team_id", Value::String(team_id.to_owned()))
            .with("detail", Value::String(detail.to_owned()));
        self.audit
            .record(event)
            .await
            .map_err(|_| internal("internal error", "internal-error"))
    }
}

/// The rendition framed exactly: its media type and length, a page's count, and bytes that start with its signature.
fn framed(rendition: Rendition, paged: bool) -> Result<Answer, Problem> {
    let length = Value::Number(Number::Int(
        i64::try_from(rendition.bytes.len()).unwrap_or(i64::MAX),
    ));
    let mut framing = vec![
        (
            "media_type".to_owned(),
            Value::String(rendition.media_type.to_owned()),
        ),
        ("length".to_owned(), length),
    ];
    let mut headers = Vec::new();
    if paged {
        let count = rendition.pages.unwrap_or_default();
        framing.push((
            "page_count".to_owned(),
            Value::Number(Number::Int(i64::from(count))),
        ));
        headers.push((PAGE_COUNT_HEADER, count.to_string()));
    }
    let framing = Value::Object(framing);
    let admitted = if paged {
        canonical_page_framing(&framing).is_some()
    } else {
        preview::canonical_framing(&framing).is_some()
    };
    let signed = preview::SIGNATURES.iter().any(|(media_type, signature)| {
        *media_type == rendition.media_type && rendition.bytes.starts_with(signature)
    });
    if !admitted || !signed {
        return Err(internal(
            "the file preview failed its framing",
            "file-preview-invalid",
        ));
    }
    Ok(Answer::Binary {
        media_type: rendition.media_type,
        body: rendition.bytes,
        headers,
    })
}

fn hex_bytes(text: &str) -> Option<Vec<u8>> {
    text.len()
        .is_multiple_of(2)
        .then(|| {
            (0..text.len())
                .step_by(2)
                .map(|index| u8::from_str_radix(text.get(index..index + 2)?, 16).ok())
                .collect()
        })
        .flatten()
}

impl Service for Files {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move {
            let team_id = call.route.param("team_id").unwrap_or_default();
            let file_id = call.route.param("file_id").unwrap_or_default();
            match call.operation() {
                Operation::FileList => self.list(team_id).await,
                Operation::FileUpload => self.upload(team_id, &call.body).await,
                Operation::FilePreview => self.preview(team_id, file_id, None).await,
                Operation::FilePagePreview => {
                    self.preview(
                        team_id,
                        file_id,
                        Some(call.route.param("page").unwrap_or_default()),
                    )
                    .await
                }
                Operation::FileContent => self.content(team_id, file_id, current_request()).await,
                Operation::FileAcknowledgement => {
                    self.acknowledge(team_id, file_id, current_request()).await
                }
                _ => Err(internal("internal error", "internal-error")),
            }
        })
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
