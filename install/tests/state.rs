//! The publication registry, icons, update transactions, and residues on disk.

use std::path::PathBuf;
use std::sync::Arc;

use strict_json::Value;
use team_install::bindings::{
    Binding, BindingError, BindingStore, LocalRecordValidator, Provenance,
};
use team_install::icons::{IconIdentity, IconStore};
use team_install::update::{ResidueStore, UpdateStore};

fn fixture() -> Value {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../protocol/install/v1/vectors.json");
    let vectors = strict_json::parse(&std::fs::read(path).expect("vectors")).expect("JSON");
    vectors
        .get("fixtures")
        .and_then(|fixtures| fixtures.get("resolve_response"))
        .and_then(|fixture| fixture.get("value"))
        .expect("fixture")
        .clone()
}

fn with(mut value: Value, key: &str, item: &str) -> Value {
    let Value::Object(members) = &mut value else {
        panic!("object")
    };
    members
        .iter_mut()
        .find(|(name, _)| name == key)
        .expect("member")
        .1 = Value::String(item.to_owned());
    value
}

fn accept_all() -> LocalRecordValidator {
    Arc::new(|_: &Value| true)
}

fn store(directory: &tempfile::TempDir) -> BindingStore {
    BindingStore::new(
        &directory.path().join("publications/bindings.json"),
        accept_all(),
    )
}

#[test]
fn a_binding_is_created_once_and_another_artifact_under_its_id_conflicts() {
    let directory = tempfile::tempdir().expect("directory");
    let bindings = store(&directory);
    let (binding, created) = bindings.put("team_1", fixture()).expect("bound");
    assert!(
        created
            && binding.binding_digest.starts_with("sha256:")
            && binding.provenance == Provenance::Published
    );
    assert_eq!(
        bindings.put("team_1", fixture()).expect("identical"),
        (binding.clone(), false)
    );
    assert_eq!(
        bindings.put("team_1", with(fixture(), "name", "Hello Again")),
        Err(BindingError::Conflict)
    );
    assert_eq!(
        bindings
            .put("team_2", fixture())
            .map(|(_, created)| created),
        Ok(true)
    );
    assert_eq!(
        bindings.get("team_1", "hello-world").expect("read"),
        Some(binding)
    );
    assert_eq!(bindings.list("team_1").expect("list").len(), 1);
    assert_eq!(bindings.snapshot().expect("snapshot").len(), 2);
}

#[test]
fn refused_documents_identities_and_the_team_limit_never_bind() {
    let directory = tempfile::tempdir().expect("directory");
    let bindings = store(&directory);
    assert_eq!(bindings.put("Team!", fixture()), Err(BindingError::Invalid));
    assert_eq!(
        bindings.put(
            "team_1",
            with(fixture(), "image_reference", "ghcr.io/other@sha256:00")
        ),
        Err(BindingError::Invalid)
    );
    assert_eq!(
        bindings.put("team_1", with(fixture(), "assistant_id", "postgres")),
        Err(BindingError::Invalid)
    );
    let refusing: LocalRecordValidator = Arc::new(|_: &Value| false);
    let local = BindingStore::new(&directory.path().join("other.json"), refusing);
    let record = strict_json::parse(br#"{"assistant_id": "local-one"}"#).expect("JSON");
    assert_eq!(
        local.put_local("team_1", record),
        Err(BindingError::Invalid)
    );
    for index in 0..16 {
        bindings
            .put(
                "team_1",
                with(fixture(), "assistant_id", &format!("assistant-{index}")),
            )
            .expect("within the limit");
    }
    assert_eq!(
        bindings.put("team_1", with(fixture(), "assistant_id", "assistant-16")),
        Err(BindingError::LimitReached)
    );
}

#[test]
fn a_tampered_registry_fails_the_whole_read() {
    let directory = tempfile::tempdir().expect("directory");
    let bindings = store(&directory);
    bindings.put("team_1", fixture()).expect("bound");
    let path = directory.path().join("publications/bindings.json");
    let written = std::fs::read_to_string(&path).expect("registry");
    assert!(written.starts_with(r#"{"bindings":[{"binding_digest":"sha256:"#));
    std::fs::write(&path, written.replace("Hello World", "Hello Wor1d")).expect("tamper");
    assert_eq!(
        bindings.get("team_1", "hello-world"),
        Err(BindingError::Invalid)
    );
    std::fs::write(&path, b"{\"version\": 2, \"bindings\": [], \"extra\": 1}").expect("tamper");
    assert_eq!(bindings.snapshot(), Err(BindingError::Invalid));
}

#[test]
fn replacement_and_deletion_require_the_current_digest() {
    let directory = tempfile::tempdir().expect("directory");
    let bindings = store(&directory);
    let (binding, _) = bindings.put("team_1", fixture()).expect("bound");
    let successor = with(fixture(), "name", "Hello Again");
    assert_eq!(
        bindings.replace("team_1", "sha256:bad", successor.clone()),
        Err(BindingError::Conflict)
    );
    let stale = format!("sha256:{}", "0".repeat(64));
    assert_eq!(
        bindings.replace("team_1", &stale, successor.clone()),
        Err(BindingError::Conflict)
    );
    let replaced = bindings
        .replace("team_1", &binding.binding_digest, successor)
        .expect("replaced");
    assert_ne!(replaced.binding_digest, binding.binding_digest);
    assert_eq!(
        bindings.delete_if_matches("team_1", "hello-world", &binding.binding_digest),
        Ok(false)
    );
    assert_eq!(
        bindings.delete_if_matches("team_1", "hello-world", &replaced.binding_digest),
        Ok(true)
    );
    assert_eq!(bindings.delete("team_1", "hello-world"), Ok(false));
}

fn icon_resolution(icon: &[u8]) -> Value {
    with(
        fixture(),
        "icon_digest",
        &format!("sha256:{}", assistant_protocol::python::sha256_hex(icon)),
    )
}

#[test]
fn an_icon_is_kept_while_pinned_or_bound_and_removed_otherwise() {
    let directory = tempfile::tempdir().expect("directory");
    let icons = IconStore::new(&directory.path().join("icons"));
    let resolution = icon_resolution(b"png");
    let identity = IconIdentity::publication(&resolution).expect("identity");
    let none = || Ok(Vec::new());
    let retained = icons.retain(identity.clone(), b"png").expect("retained");
    assert!(icons.retain(identity.clone(), b"other").is_err());
    assert_eq!(icons.read(&identity).expect("read"), b"png");
    retained.release(&none).expect("released");
    assert!(icons.read(&identity).is_err());
    let bindings = store(&directory);
    let (binding, _) = bindings.put("team_1", resolution).expect("bound");
    let bound = binding.clone();
    let references = move || -> Result<Vec<Binding>, BindingError> { Ok(vec![bound.clone()]) };
    icons
        .retain(identity.clone(), b"png")
        .expect("retained")
        .release(&references)
        .expect("kept");
    assert_eq!(icons.read(&identity).expect("still bound"), b"png");
    icons
        .retire(&binding, &references, || Ok(()))
        .expect("retired");
    assert!(icons.read(&identity).is_err());
}

#[test]
fn an_update_is_one_durable_transaction_per_assistant() {
    let directory = tempfile::tempdir().expect("directory");
    let bindings = store(&directory);
    let (previous, _) = bindings.put("team_1", fixture()).expect("bound");
    let updates = UpdateStore::new(&directory.path().join("updates"), &bindings);
    let image = format!("sha256:{}", "7".repeat(64));
    assert_eq!(
        updates.begin(&previous, fixture(), &image),
        Err(BindingError::Conflict)
    );
    assert_eq!(
        updates.begin(
            &previous,
            with(fixture(), "assistant_id", "other-one"),
            &image
        ),
        Err(BindingError::Conflict)
    );
    let update = updates
        .begin(&previous, with(fixture(), "name", "Hello Again"), &image)
        .expect("begun");
    assert_eq!(
        updates.begin(&previous, with(fixture(), "name", "Hello Again"), &image),
        Ok(update.clone())
    );
    assert_eq!(
        updates.begin(&previous, with(fixture(), "name", "Hello Third"), &image),
        Err(BindingError::Conflict)
    );
    assert_eq!(
        updates.get("team_1", "hello-world"),
        Ok(Some(update.clone()))
    );
    assert_eq!(updates.list(), Ok(vec![update.clone()]));
    updates.clear(&update).expect("cleared");
    assert_eq!(updates.get("team_1", "hello-world"), Ok(None));
    updates
        .clear(&update)
        .expect("an absent transaction is already cleared");
}

#[test]
fn a_residue_queues_one_exact_image_until_cleared() {
    let directory = tempfile::tempdir().expect("directory");
    let image = format!("sha256:{}", "7".repeat(64));
    let residues = ResidueStore::new(&directory.path().join("residues"));
    residues.add(&image).expect("queued");
    residues.add(&image).expect("idempotent");
    assert_eq!(residues.list(), Ok(vec![image.clone()]));
    residues.clear(&image).expect("cleared");
    assert_eq!(residues.list(), Ok(Vec::new()));
    assert_eq!(residues.add("sha256:short"), Err(BindingError::Invalid));
}
