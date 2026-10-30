//! Team lifecycle and Team storage over the controller's HTTP surface: create, rename, upload, restart, and a Team
//! whose network the daemon lost.

use crate::api::{Api, Body, sha256_hex};
use crate::docker;
use crate::flow::Names;
use crate::json;
use crate::probe::{self, Mount};
use crate::report::{Outcome, Report, document};
use crate::setup::{self, Started};

fn team_name(name: &str) -> Outcome<Body> {
    Ok(Body::Json(document(&format!(
        r#"{{"team_name":"{name}"}}"#
    ))?))
}

/// A file upload.
pub fn file(content: &[u8], filename: &str, media_type: &str) -> Body {
    Body::File {
        content: content.to_vec(),
        filename: filename.to_owned(),
        media_type: media_type.to_owned(),
    }
}

/// Whether `path` below the storage volume exists, read through the volume.
pub fn stored(names: &Names, test: &str, path: &str) -> Outcome<bool> {
    let volume = names.volume("storage");
    let mounts = [Mount {
        volume: &volume,
        target: "/storage",
        writable: false,
    }];
    Ok(probe::shell(&mounts, &[], &format!("test {test} '/storage/{path}'"))?.success)
}

/// Creating a Team is idempotent, and a rename changes only its display name (ADR-0088).
pub fn create_and_rename(api: &Api<'_>, report: &mut Report) -> Outcome {
    let created = api.ok(
        "POST",
        "/v1/teams/demo_team/create",
        &team_name("Demo Team")?,
    )?;
    report.equal(
        "a new Team is created",
        json::boolean(&created, &["created"]),
        Some(true),
    )?;
    let again = api.ok(
        "POST",
        "/v1/teams/demo_team/create",
        &team_name("Demo Team")?,
    )?;
    report.equal(
        "creating it again confirms it without recreating it",
        json::boolean(&again, &["created"]),
        Some(false),
    )?;
    let teams = api.ok("GET", "/v1/teams", &Body::None)?;
    report.same(
        "the Team is listed running under its name",
        json::at(&teams, &["teams"]).unwrap_or(&strict_json::Value::Null),
        r#"[{"team_id":"demo_team","team_name":"Demo Team","status":"running"}]"#,
    )?;
    rename(api, report)
}

/// A rename keeps the Team id; the former name then conflicts with it.
fn rename(api: &Api<'_>, report: &mut Report) -> Outcome {
    let renamed = api.ok("PATCH", "/v1/teams/demo_team", &team_name("Demo Renamed")?)?;
    report.equal(
        "a rename keeps the Team id and changes its name",
        (
            json::text(&renamed, &["team_id"]),
            json::text(&renamed, &["team_name"]),
        ),
        (Some("demo_team"), Some("Demo Renamed")),
    )?;
    let teams = api.ok("GET", "/v1/teams", &Body::None)?;
    report.equal(
        "the list shows the new name",
        json::items(&teams, &["teams"])
            .first()
            .and_then(|team| json::text(team, &["team_name"])),
        Some("Demo Renamed"),
    )?;
    let (status, conflict) = api.call(
        "POST",
        "/v1/teams/demo_team/create",
        &team_name("Demo Team")?,
    )?;
    report.equal(
        "the old name no longer creates this id",
        (status, json::text(&conflict, &["code"])),
        (409, Some("team-name-conflict")),
    )
}

/// An uploaded file is listed, counted, stored in the Team's own database, and survives a controller restart.
pub fn upload_and_restart(api: &Api<'_>, names: &Names, report: &mut Report) -> Outcome<Started> {
    let uploaded = api.ok(
        "POST",
        "/v1/teams/demo_team/files",
        &file(b"Team private data", "brief.txt", "text/plain"),
    )?;
    let file_id = json::text(&uploaded, &["file", "id"])
        .unwrap_or_default()
        .to_owned();
    report.check(
        "an upload gets an opaque 32-hex id",
        file_id.len() == 32
            && file_id
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')),
        &file_id,
    )?;
    report.equal(
        "the Team's storage limit is 100 MiB",
        json::integer(&uploaded, &["file", "limit_bytes"]),
        Some(100 * 1024 * 1024),
    )?;
    let files = api.ok("GET", "/v1/teams/demo_team/files", &Body::None)?;
    report.equal(
        "the file is listed and counted",
        (
            json::items(&files, &["files"])
                .first()
                .and_then(|file| json::text(file, &["id"])),
            json::integer(&files, &["used_bytes"]),
        ),
        (Some(file_id.as_str()), Some(17)),
    )?;
    report.check(
        "the file lives in the Team's own database",
        stored(names, "-f", "demo_team/files.sqlite3")?,
        "storage/demo_team/files.sqlite3",
    )?;
    docker::ok(&["restart", &names.controller])?;
    let restarted = setup::wait_controller(names)?;
    let api = setup::api(&restarted, api.supervisor);
    let files = api.ok("GET", "/v1/teams/demo_team/files", &Body::None)?;
    report.equal(
        "the file survives a controller restart",
        json::items(&files, &["files"])
            .first()
            .and_then(|file| json::text(file, &["id"])),
        Some(file_id.as_str()),
    )?;
    Ok(restarted)
}

/// A daemon-side network loss never lets a new lifecycle of the Team inherit its old opaque data.
pub fn lost_network(api: &Api<'_>, names: &Names, report: &mut Report) -> Outcome {
    api.ok(
        "POST",
        "/v1/teams/orphan_team/create",
        &team_name("Orphan Team")?,
    )?;
    api.ok(
        "POST",
        "/v1/teams/orphan_team/files",
        &file(b"must not survive", "stale.txt", "application/octet-stream"),
    )?;
    let prefix = &sha256_hex(names.space_id.as_bytes())[..12];
    docker::ok(&[
        "network",
        "rm",
        &format!("shimpz-local-{prefix}-team-orphan_team"),
    ])?;
    let recreated = api.ok(
        "POST",
        "/v1/teams/orphan_team/create",
        &team_name("Orphan Team")?,
    )?;
    report.equal(
        "a Team whose network the daemon lost is created anew",
        json::boolean(&recreated, &["created"]),
        Some(true),
    )?;
    let files = api.ok("GET", "/v1/teams/orphan_team/files", &Body::None)?;
    report.check(
        "the new lifecycle inherits none of the old files",
        json::items(&files, &["files"]).is_empty(),
        &files,
    )?;
    api.ok(
        "DELETE",
        "/v1/teams/orphan_team",
        &team_name("Orphan Team")?,
    )?;
    Ok(())
}
