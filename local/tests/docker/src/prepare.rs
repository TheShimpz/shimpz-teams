//! The preparation helper on a real daemon, through the file preview routes: a hostile corpus is refused within the
//! helper's bounds, legitimate files render as sanitized images, the helper runs inside its exact envelope, and no
//! helper outlives its preview.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread;
use std::time::Duration;

use strict_json::Value;

use crate::api::{Api, Body};
use crate::docker::{self, Args};
use crate::flow::Names;
use crate::http::Response;
use crate::json;
use crate::report::{Outcome, Report, fail};
use crate::storage::file;

/// The corpus: each file, its name, and its media type.
const CORPUS: [(&str, &[u8], &str); 7] = [
    (
        "photo.jpg",
        include_bytes!("../fixtures/prepare/photo.jpg"),
        "image/jpeg",
    ),
    (
        "report.pdf",
        include_bytes!("../fixtures/prepare/report.pdf"),
        "application/pdf",
    ),
    (
        "secret.pdf",
        include_bytes!("../fixtures/prepare/secret.pdf"),
        "application/pdf",
    ),
    (
        "pageless.pdf",
        include_bytes!("../fixtures/prepare/pageless.pdf"),
        "application/pdf",
    ),
    (
        "moving.webp",
        include_bytes!("../fixtures/prepare/moving.webp"),
        "image/webp",
    ),
    (
        "bomb.png",
        include_bytes!("../fixtures/prepare/bomb.png"),
        "image/png",
    ),
    (
        "truncated.jpg",
        include_bytes!("../fixtures/prepare/truncated.jpg"),
        "image/jpeg",
    ),
];
/// The Team the corpus is uploaded to.
const TEAM: &str = "prepare_team";
/// A JPEG's start, and a PNG's.
const JPEG: &[u8] = b"\xff\xd8\xff";
const PNG: &[u8] = b"\x89PNG\r\n\x1a\n";

/// Each file's id after upload.
fn upload(api: &Api<'_>) -> Outcome<Vec<String>> {
    api.ok(
        "POST",
        &format!("/v1/teams/{TEAM}/create"),
        &Body::Json(crate::report::document(r#"{"team_name":"Prepare Team"}"#)?),
    )?;
    let mut ids = Vec::new();
    for (name, content, media_type) in CORPUS {
        let uploaded = api.ok(
            "POST",
            &format!("/v1/teams/{TEAM}/files"),
            &file(content, name, media_type),
        )?;
        ids.push(
            json::text(&uploaded, &["file", "id"])
                .unwrap_or_default()
                .to_owned(),
        );
    }
    Ok(ids)
}

fn preview(api: &Api<'_>, id: &str, page: Option<u32>) -> Outcome<Response> {
    let path = page.map_or_else(
        || format!("/v1/teams/{TEAM}/files/{id}/preview"),
        |page| format!("/v1/teams/{TEAM}/files/{id}/pages/{page}/preview"),
    );
    api.send("GET", &path, &Body::None)
}

/// The width and height a baseline or progressive JPEG's frame header declares.
fn jpeg_size(raw: &[u8]) -> Option<(u16, u16)> {
    let mut offset = 2;
    while offset + 9 < raw.len() {
        if raw[offset] != 0xff {
            return None;
        }
        let marker = raw[offset + 1];
        let length = usize::from(u16::from_be_bytes([raw[offset + 2], raw[offset + 3]]));
        if matches!(marker, 0xc0..=0xc3) {
            let height = u16::from_be_bytes([raw[offset + 5], raw[offset + 6]]);
            let width = u16::from_be_bytes([raw[offset + 7], raw[offset + 8]]);
            return Some((width, height));
        }
        offset += 2 + length;
    }
    None
}

fn refused(response: &Response) -> (u16, Option<String>) {
    let code = response
        .json()
        .ok()
        .and_then(|body| json::text(&body, &["code"]).map(str::to_owned));
    (response.status, code)
}

/// Legitimate files render as sanitized images; the EXIF orientation is applied.
fn renditions(api: &Api<'_>, ids: &[String], report: &mut Report) -> Outcome {
    let photo = preview(api, &ids[0], None)?;
    report.equal(
        "a photo previews as a JPEG",
        (
            photo.status,
            photo.header("content-type"),
            photo.body.starts_with(JPEG),
        ),
        (200, Some("image/jpeg"), true),
    )?;
    report.check(
        "its rendition is upright (EXIF orientation applied) and smaller than the original",
        jpeg_size(&photo.body).is_some_and(|(width, height)| height > width && width < 1800),
        jpeg_size(&photo.body),
    )?;
    report.equal(
        "it is framed by its exact length",
        photo.header("content-length").map(str::to_owned),
        Some(photo.body.len().to_string()),
    )?;
    for page in [1, 2] {
        let rendered = preview(api, &ids[1], Some(page))?;
        let image = rendered.body.starts_with(JPEG) || rendered.body.starts_with(PNG);
        report.equal(
            &format!(
                "page {page} of a text PDF previews as an image with the document's page count"
            ),
            (rendered.status, image, rendered.header("x-page-count")),
            (200, true, Some("2")),
        )?;
    }
    Ok(())
}

/// Every hostile file is refused as having no preview, and a page past the end is not found.
fn hostile(api: &Api<'_>, ids: &[String], report: &mut Report) -> Outcome {
    let unsupported = (422, Some("file-preview-unsupported".to_owned()));
    for (index, page, description) in [
        (2, Some(1), "an encrypted PDF"),
        (3, Some(1), "a PDF without pages"),
        (5, None, "a decompression bomb"),
        (6, None, "a truncated JPEG"),
        (4, None, "an animated WebP"),
        (1, None, "a PDF asked for as one image"),
    ] {
        let response = preview(api, &ids[index], page)?;
        report.equal(
            &format!("{description} has no preview"),
            refused(&response),
            unsupported.clone(),
        )?;
    }
    let past = preview(api, &ids[1], Some(3))?;
    report.equal(
        "a page past a PDF's end has no preview",
        refused(&past).0,
        422,
    )?;
    Ok(())
}

/// Watches the daemon for the Space's preparation helper and keeps the first inspection of one.
struct Watch {
    stop: Arc<AtomicBool>,
    seen: Arc<Mutex<Option<Value>>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Watch {
    fn start(names: &Names) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let seen = Arc::new(Mutex::new(None));
        let filter = format!("label=com.shimpz.local.space-id={}", names.space_id);
        let thread = {
            let (stop, seen) = (Arc::clone(&stop), Arc::clone(&seen));
            thread::spawn(move || {
                while !stop.load(Ordering::SeqCst) {
                    let listed = docker::ok(&[
                        "ps",
                        "--quiet",
                        "--filter",
                        &filter,
                        "--filter",
                        "label=com.shimpz.local.kind=prepare",
                    ]);
                    if let Some(id) = listed
                        .ok()
                        .and_then(|ids| ids.lines().next().map(str::to_owned))
                        && let Ok(inspected) = docker::inspect(&["inspect", &id])
                    {
                        *seen.lock().unwrap_or_else(PoisonError::into_inner) = Some(inspected);
                        return;
                    }
                    thread::sleep(Duration::from_millis(10));
                }
            })
        };
        Self {
            stop,
            seen,
            thread: Some(thread),
        }
    }

    fn finish(mut self) -> Option<Value> {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()
    }
}

/// One helper caught while it prepares a file runs offline, read-only, unprivileged, bounded, without mounts or a log,
/// as the nobody identity, on the controller's own image.
fn envelope(api: &Api<'_>, names: &Names, ids: &[String], report: &mut Report) -> Outcome {
    let mut caught = None;
    for _ in 0..5 {
        let watch = Watch::start(names);
        preview(api, &ids[0], None)?;
        caught = watch.finish();
        if caught.is_some() {
            break;
        }
    }
    let Some(helper) = caught else {
        return fail("no preparation helper was seen while a photo was prepared");
    };
    inspected(&helper, names, report)?;
    report.check(
        "it has no binds or mounts",
        json::empty(&helper, &["HostConfig", "Binds"]) && json::empty(&helper, &["Mounts"]),
        &helper,
    )
}

/// The caught helper's envelope.
fn inspected(helper: &Value, names: &Names, report: &mut Report) -> Outcome {
    let controller_image = docker::ok(&["inspect", "--format", "{{.Image}}", &names.controller])?;
    let host = |field: &str| {
        json::at(helper, &["HostConfig", field])
            .cloned()
            .unwrap_or(Value::Null)
    };
    report.equal(
        "the helper runs as nobody on the controller's own image, without a network",
        (
            json::text(helper, &["Config", "User"]),
            json::text(helper, &["Image"]),
            json::text(helper, &["HostConfig", "NetworkMode"]),
        ),
        (
            Some("65534:65534"),
            Some(controller_image.as_str()),
            Some("none"),
        ),
    )?;
    report.equal(
        "its root is read-only and every capability is dropped",
        (
            json::boolean(helper, &["HostConfig", "ReadonlyRootfs"]),
            json::strings(helper, &["HostConfig", "CapDrop"]),
        ),
        (Some(true), vec!["ALL"]),
    )?;
    report.equal(
        "it is bounded to 256 MiB without swap and 128 processes, with 64 open files and no log",
        (
            json::integer(helper, &["HostConfig", "Memory"]),
            json::integer(helper, &["HostConfig", "MemorySwap"]),
            json::integer(helper, &["HostConfig", "PidsLimit"]),
            json::text(helper, &["HostConfig", "LogConfig", "Type"]),
        ),
        (
            Some(268_435_456),
            Some(268_435_456),
            Some(128),
            Some("none"),
        ),
    )?;
    report.same(
        "its open files are limited to 64",
        &host("Ulimits"),
        r#"[{"Name":"nofile","Soft":64,"Hard":64}]"#,
    )
}

/// No helper outlives the previews; the Team and its files then go.
fn residue(api: &Api<'_>, names: &Names, report: &mut Report) -> Outcome {
    let left = Args::default()
        .words("ps --all --quiet")
        .flag(
            "--filter",
            format!("label=com.shimpz.local.space-id={}", names.space_id),
        )
        .flag("--filter", "label=com.shimpz.local.kind=prepare")
        .ok()?;
    report.equal(
        "no preparation helper outlives its preview",
        left.as_str(),
        "",
    )?;
    api.ok(
        "DELETE",
        &format!("/v1/teams/{TEAM}"),
        &Body::Json(crate::report::document(r#"{"team_name":"Prepare Team"}"#)?),
    )?;
    Ok(())
}

/// The preparation helper's real-daemon proof.
pub fn hostile_corpus(api: &Api<'_>, names: &Names, report: &mut Report) -> Outcome {
    let ids = upload(api)?;
    renditions(api, &ids, report)?;
    hostile(api, &ids, report)?;
    envelope(api, names, &ids, report)?;
    residue(api, names, report)
}

#[cfg(test)]
mod tests {
    use super::jpeg_size;

    #[test]
    fn a_jpeg_frame_header_names_its_size() {
        let header = [
            0xff, 0xd8, 0xff, 0xe0, 0x00, 0x04, 0x00, 0x00, 0xff, 0xc0, 0x00, 0x0b, 0x08, 0x01,
            0x00, 0x00, 0xc8, 0x01, 0x00, 0x00,
        ];
        assert_eq!(jpeg_size(&header), Some((200, 256)));
        assert_eq!(jpeg_size(b"\xff\xd8\x00"), None);
    }
}
