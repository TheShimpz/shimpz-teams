//! The Local [`Preparer`]: one message's selected files prepared for the Brain, and one listed file's sanitized
//! rendition for the Supervisor's browser, both through the controller's one preparation slot and its helper.
//!
//! Text is read here; images and PDFs go to the helper, whose answers are re-validated by `team_prepare`. A turn's
//! Stop leaves the wait for the slot at once, reads no further file, and removes a running helper; a preview ended by
//! its Team's teardown or the Space reset does the same through its [`Preview`]. The helper is always gone before the
//! answer returns.

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_prepare::answer::MediaType;
use team_prepare::content;
use team_prepare::detect::{self, Kind as Detected};
use team_prepare::limits::{MAX_IMAGE_BYTES, MAX_PDF_BYTES};
use team_prepare::message::{self, Attachment, LimitError, MAX_SOURCE_BYTES, Plan};
use team_prepare::request::Kind;
use team_storage::files::{FileMetadata, StorageError};

use super::admission::{Admission, PreviewWait};
use super::session::{Ended, Helpers, Hooks, Session, SessionError};
use super::store::Store;
use super::{PrepareError, Prepared, Preparer, Rendition};
use crate::coordination::previews::Preview;

/// Why a segment was not prepared.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SegmentError {
    /// The turn was stopped.
    Stopped,
    /// The message exceeds a per-message ceiling.
    Limit(LimitError),
    /// A selected file no longer has the bytes its turn bound.
    Changed,
    /// A helper could not be started or removed.
    Unavailable,
    /// Team storage refused or failed.
    Store(StorageError),
}

/// Why a rendition was not prepared.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RenditionError {
    /// The preview was ended, or the file changed while it was read.
    Ended,
    /// The preparation slot stayed busy.
    Busy,
    /// A helper could not be started or removed.
    Unavailable,
    /// Team storage refused or failed.
    Store(StorageError),
}

/// The Local preparer.
pub struct LocalPreparer {
    helpers: Helpers,
    store: Store,
    admission: Admission,
}

impl std::fmt::Debug for LocalPreparer {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("LocalPreparer")
            .field("helpers", &self.helpers)
            .finish_non_exhaustive()
    }
}

struct TurnHooks<'a>(&'a Stop);

impl Hooks for TurnHooks<'_> {
    fn started(&self, _id: &str) -> Result<(), Ended> {
        if self.0.is_stopped() {
            Err(Ended)
        } else {
            Ok(())
        }
    }

    fn stopped(&self, _id: &str) {}
}

struct PreviewHooks<'a>(&'a Preview);

impl Hooks for PreviewHooks<'_> {
    fn started(&self, id: &str) -> Result<(), Ended> {
        self.0.started(id).map_err(|_| Ended)
    }

    fn stopped(&self, id: &str) {
        self.0.stopped(id);
    }
}

impl LocalPreparer {
    /// A preparer over `store`, with its own preparation slot.
    #[must_use]
    pub fn new(helpers: Helpers, store: Store) -> Self {
        Self {
            helpers,
            store,
            admission: Admission::new(),
        }
    }

    /// Prepares every selected file of one message in order, holding the slot from before the first original is
    /// read until the helper is gone, and refuses a message beyond a per-message ceiling.
    ///
    /// # Errors
    /// [`SegmentError`] when the segment is not prepared.
    pub async fn attachments(
        &self,
        team_id: &str,
        selected: &[FileMetadata],
        stop: &Stop,
    ) -> Result<Vec<Attachment>, SegmentError> {
        let sizes: Vec<u64> = selected.iter().map(|item| size(item.size)).collect();
        message::admit_selection(&sizes).map_err(SegmentError::Limit)?;
        let _slot = self
            .admission
            .turn(stop)
            .await
            .map_err(|_| SegmentError::Stopped)?;
        let hooks = TurnHooks(stop);
        let mut session = Session::new(&self.helpers, team_id, &hooks);
        let prepared = tokio::select! {
            biased;
            () = stop.stopped() => Err(SegmentError::Stopped),
            prepared = self.each(team_id, selected, stop, &mut session) => prepared,
        };
        let closed = session.close().await;
        let prepared = prepared?;
        closed.map_err(|_| SegmentError::Unavailable)?;
        if stop.is_stopped() {
            return Err(SegmentError::Stopped);
        }
        message::admit_message(&prepared).map_err(SegmentError::Limit)?;
        Ok(prepared)
    }

    async fn each(
        &self,
        team_id: &str,
        selected: &[FileMetadata],
        stop: &Stop,
        session: &mut Session<'_>,
    ) -> Result<Vec<Attachment>, SegmentError> {
        let mut prepared = Vec::new();
        for item in selected {
            if stop.is_stopped() {
                return Err(SegmentError::Stopped);
            }
            prepared.push(self.one(team_id, item, session).await?);
        }
        Ok(prepared)
    }

    /// One selected file's attachment; an original beyond every readable kind's ceiling is never read.
    async fn one(
        &self,
        team_id: &str,
        item: &FileMetadata,
        session: &mut Session<'_>,
    ) -> Result<Attachment, SegmentError> {
        if size(item.size) > u64::try_from(MAX_SOURCE_BYTES).unwrap_or(u64::MAX) {
            return Ok(message::unread(
                &item.id,
                &item.name,
                size(item.size),
                &item.sha256,
            ));
        }
        let data = self
            .verified(team_id, item)
            .await
            .map_err(|error| match error {
                Read::Changed => SegmentError::Changed,
                Read::Store(error) => SegmentError::Store(error),
            })?;
        let found = detect::detect(&item.name, &data);
        let content = match message::plan(&data, found) {
            Plan::Ready(content) => content,
            Plan::Helper(kind) => {
                let answer =
                    session
                        .prepare(kind, &data, None)
                        .await
                        .map_err(|error| match error {
                            SessionError::Ended => SegmentError::Stopped,
                            SessionError::Unavailable => SegmentError::Unavailable,
                        })?;
                if kind == Kind::Pdf {
                    content::pdf(&answer)
                } else {
                    content::image(&answer)
                }
            }
        };
        Ok(Attachment {
            id: item.id.clone(),
            name: item.name.clone(),
            media_type: found.media_type.to_owned(),
            size: size(item.size),
            sha256: item.sha256.clone(),
            content,
        })
    }

    /// One file's bytes, refused when they are no longer the bytes `item` names.
    async fn verified(&self, team_id: &str, item: &FileMetadata) -> Result<Vec<u8>, Read> {
        let (stored, data) = self
            .store
            .read(team_id, &item.id)
            .await
            .map_err(Read::Store)?;
        if stored.metadata.sha256 != item.sha256 || stored.metadata.size != item.size {
            return Err(Read::Changed);
        }
        Ok(data)
    }

    /// One listed file's sanitized rendition: without `page`, a JPEG, PNG, or static WebP's; with `page` (from 1),
    /// that page of a PDF, with its page count. `None` when it has none. The kind comes from the bytes, an original
    /// beyond its ceiling is never read, and the slot is held from before the read until the helper is gone.
    ///
    /// # Errors
    /// [`RenditionError`] when it is not prepared.
    pub async fn render(
        &self,
        team_id: &str,
        item: &FileMetadata,
        page: Option<i64>,
        preview: &Preview,
    ) -> Result<Option<Rendition>, RenditionError> {
        let (kind, ceiling) = if page.is_none() {
            (Detected::Image, MAX_IMAGE_BYTES)
        } else {
            (Detected::Pdf, MAX_PDF_BYTES)
        };
        if size(item.size) > u64::try_from(ceiling).unwrap_or(u64::MAX) {
            return Ok(None);
        }
        let _slot = self
            .admission
            .preview(preview)
            .await
            .map_err(|wait| match wait {
                PreviewWait::Busy => RenditionError::Busy,
                PreviewWait::Ended => RenditionError::Ended,
            })?;
        preview.check().map_err(|_| RenditionError::Ended)?;
        let data = self
            .verified(team_id, item)
            .await
            .map_err(|error| match error {
                Read::Changed => RenditionError::Ended,
                Read::Store(error) => RenditionError::Store(error),
            })?;
        if detect::detect(&item.name, &data).kind != kind || data.len() > ceiling {
            return Ok(None);
        }
        preview.check().map_err(|_| RenditionError::Ended)?;
        let answer = self.drawn(team_id, &data, page, preview).await?;
        drop(data);
        preview.check().map_err(|_| RenditionError::Ended)?;
        Ok(rendition(&answer, page))
    }

    async fn drawn(
        &self,
        team_id: &str,
        data: &[u8],
        page: Option<i64>,
        preview: &Preview,
    ) -> Result<Value, RenditionError> {
        let hooks = PreviewHooks(preview);
        let mut session = Session::new(&self.helpers, team_id, &hooks);
        let kind = if page.is_none() {
            Kind::Image
        } else {
            Kind::PdfPage
        };
        let answer = session.prepare(kind, data, page).await;
        let closed = session.close().await;
        let answer = answer.map_err(|error| match error {
            SessionError::Ended => RenditionError::Ended,
            SessionError::Unavailable => RenditionError::Unavailable,
        })?;
        closed.map_err(|_| RenditionError::Unavailable)?;
        Ok(answer)
    }
}

enum Read {
    Changed,
    Store(StorageError),
}

/// A stored size, which the store keeps non-negative.
fn size(stored: i64) -> u64 {
    u64::try_from(stored).unwrap_or(u64::MAX)
}

/// The validated rendition of a helper answer, or `None`.
fn rendition(answer: &Value, page: Option<i64>) -> Option<Rendition> {
    let (content, pages) = match page {
        None => (content::image(answer), None),
        Some(page) => {
            let (content, count) = content::page(answer, page)?;
            (content, Some(u32::try_from(count).ok()?))
        }
    };
    let media_type = match content.get("media_type") {
        Some(Value::String(name)) => MediaType::parse(name)?,
        _ => return None,
    };
    let bytes = match content.get("base64") {
        Some(Value::String(encoded)) => content::decoded(encoded)?,
        _ => return None,
    };
    Some(Rendition {
        media_type: media_type.name(),
        bytes,
        pages,
    })
}

impl Preparer for LocalPreparer {
    fn prepare_segment<'a>(
        &'a self,
        team_id: &'a str,
        file_ids: &'a [String],
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<Vec<Prepared>, PrepareError>> {
        Box::pin(async move {
            let mut selected = Vec::new();
            for file_id in file_ids {
                let item = self
                    .store
                    .listed(team_id, file_id)
                    .await
                    .map_err(|_| PrepareError::Unreadable)?;
                selected.push(item);
            }
            let attachments =
                self.attachments(team_id, &selected, stop)
                    .await
                    .map_err(|error| match error {
                        SegmentError::Stopped => PrepareError::Stopped,
                        SegmentError::Unavailable => PrepareError::Unavailable,
                        SegmentError::Limit(_) | SegmentError::Changed | SegmentError::Store(_) => {
                            PrepareError::Unreadable
                        }
                    })?;
            Ok(attachments
                .iter()
                .map(|attachment| Prepared {
                    file_id: attachment.id.clone(),
                    derivative: attachment.wire(),
                })
                .collect())
        })
    }

    fn rendition<'a>(
        &'a self,
        team_id: &'a str,
        file_id: &'a str,
        page: Option<u32>,
        preview: &'a Preview,
    ) -> BoxFuture<'a, Result<Rendition, PrepareError>> {
        Box::pin(async move {
            let item = self
                .store
                .listed(team_id, file_id)
                .await
                .map_err(|_| PrepareError::Unreadable)?;
            let rendered = self
                .render(team_id, &item, page.map(i64::from), preview)
                .await
                .map_err(|error| match error {
                    RenditionError::Ended => PrepareError::Stopped,
                    RenditionError::Busy | RenditionError::Unavailable => PrepareError::Unavailable,
                    RenditionError::Store(_) => PrepareError::Unreadable,
                })?;
            rendered.ok_or(PrepareError::Unreadable)
        })
    }
}

#[cfg(test)]
mod tests;
