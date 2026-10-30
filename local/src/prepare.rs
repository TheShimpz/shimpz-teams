//! The preparation seam (frozen in W0; workstream W4b implements it): chat segments and file previews reach the
//! networkless helper only through a [`Preparer`].
//!
//! The helper runs the controller's own image as `shimpz-team prepare-idle`, one per Space at a time, and is inspected
//! against its complete envelope before any file byte reaches it; each file is one `shimpz-team prepare` exec.

pub mod admission;
pub mod exchange;
#[cfg(test)]
mod fakes;
pub mod files;
pub mod helper;
pub mod idle;
pub mod service;
pub mod session;
pub mod store;
pub mod teardown;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_prepare::message::LimitError;

use crate::coordination::previews::Preview;

/// One prepared file of a segment, as the Brain receives it in the turn's `attachments`.
#[derive(Clone, Debug, PartialEq)]
pub struct Prepared {
    /// The file's opaque id.
    pub file_id: String,
    /// The derivative the helper answered (`text`, an `image`, or `unreadable` with its reason).
    pub derivative: Value,
}

/// One sanitized image rendition for the Supervisor's browser.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rendition {
    /// `image/jpeg` or `image/png`.
    pub media_type: &'static str,
    /// The bytes.
    pub bytes: Vec<u8>,
    /// For a PDF page, the document's page count.
    pub pages: Option<u32>,
}

/// A closed preparation failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrepareError {
    /// The Stop was set or the preview was ended.
    Stopped,
    /// The file cannot be rendered (refused by the helper's closed rules).
    Unreadable,
    /// Docker, the helper, or Team storage is unavailable.
    Unavailable,
    /// The preparation slot stayed busy beyond a preview's queue or deadline; the caller retries later.
    Busy,
    /// The message exceeds a per-message ceiling; [`LimitError::code`] names it (`attachments-too-*`).
    Limit(LimitError),
    /// A selected file no longer has the bytes its turn bound.
    Changed,
    /// A file is not listed: absent, collected, or idle past its grace.
    NotFound,
}

/// Prepares files in the helper.
pub trait Preparer: Send + Sync {
    /// Prepares every selected file of one chat segment, in order, under the turn's `stop`.
    ///
    /// # Errors
    /// [`PrepareError`] when the segment cannot be prepared.
    fn prepare_segment<'a>(
        &'a self,
        team_id: &'a str,
        file_ids: &'a [String],
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<Vec<Prepared>, PrepareError>>;

    /// Renders one listed file (or one page of a PDF) under its open `preview`.
    ///
    /// # Errors
    /// [`PrepareError`] when it cannot be rendered.
    fn rendition<'a>(
        &'a self,
        team_id: &'a str,
        file_id: &'a str,
        page: Option<u32>,
        preview: &'a Preview,
    ) -> BoxFuture<'a, Result<Rendition, PrepareError>>;
}
