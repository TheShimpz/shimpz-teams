//! Files from the web: their mark, and the acknowledgement a person gives one.

use super::*;

fn acknowledge(store: &FileStore, file: &str, who: &str) -> Result<(), StorageError> {
    store.acknowledge("team_1", file, who, |_| Ok::<(), StorageError>(()))
}

#[test]
fn a_web_mark_survives_an_identical_upload_and_goes_with_its_file() {
    let fixture = Fixture::new();
    let store = fixture.store(super::super::DEFAULT_LIMIT_BYTES);
    let stored = store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), true)
        .expect("stored");
    let id = stored.file.metadata.id.as_str();
    let again = store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), false)
        .expect("stored");
    assert_eq!(again.file.metadata.id, id);
    assert_eq!(store.released("team_1", id, None), Ok(false));
    acknowledge(&store, id, SUPERVISOR).expect("acknowledged");
    assert_eq!(store.released("team_1", id, Some(SUPERVISOR)), Ok(true));
    assert_eq!(
        store.released("team_1", id, Some(&"b".repeat(32))),
        Ok(false)
    );
    let upload = store
        .put("team_1", "nota.txt", b"plain", Some("text/plain"), false)
        .expect("stored");
    assert_eq!(
        store.released("team_1", &upload.file.metadata.id, None),
        Ok(true)
    );
    let not_web = acknowledge(&store, &upload.file.metadata.id, SUPERVISOR).expect_err("refused");
    assert_eq!(
        (not_web.kind, not_web.message),
        (
            StorageErrorKind::NotWeb,
            "the file did not come from the web"
        )
    );
    // Collected with its file, the acknowledgement is gone too.
    fixture.clock.advance(GRACE + Duration::from_secs(1));
    store.settle("team_1", &[]).expect("settled");
    assert_eq!(
        kind(store.released("team_1", id, Some(SUPERVISOR))),
        StorageErrorKind::NotFound
    );
    assert_eq!(
        kind(acknowledge(&store, id, SUPERVISOR)),
        StorageErrorKind::NotFound
    );
}

#[test]
fn the_web_mark_follows_the_bytes_whatever_their_name() {
    let fixture = Fixture::new();
    let store = fixture.store(super::super::DEFAULT_LIMIT_BYTES);
    let upload = store
        .put("team_1", "nota.pdf", PDF, Some("application/pdf"), false)
        .expect("stored");
    assert_eq!(
        store.released("team_1", &upload.file.metadata.id, None),
        Ok(true)
    );
    store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), true)
        .expect("stored");
    // The earlier upload of the same bytes is marked with them, and so is a later one under any name.
    assert_eq!(
        store.released("team_1", &upload.file.metadata.id, None),
        Ok(false)
    );
    let later = store
        .put("team_1", "copia.bin", PDF, None, false)
        .expect("stored");
    assert_eq!(
        store.released("team_1", &later.file.metadata.id, None),
        Ok(false)
    );
    let other = store
        .put(
            "team_1",
            "outro.txt",
            b"other bytes",
            Some("text/plain"),
            false,
        )
        .expect("stored");
    assert_eq!(
        store.released("team_1", &other.file.metadata.id, None),
        Ok(true)
    );
}

#[test]
fn no_acknowledgement_exists_without_its_audit_or_outside_its_form() {
    let fixture = Fixture::new();
    let store = fixture.store(super::super::DEFAULT_LIMIT_BYTES);
    assert_eq!(
        kind(store.released("team_1", &"f".repeat(32), Some(SUPERVISOR))),
        StorageErrorKind::NotFound
    );
    let stored = store
        .put("team_1", "edital.pdf", PDF, Some("application/pdf"), true)
        .expect("stored");
    let id = stored.file.metadata.id.as_str();
    let audited = store.acknowledge("team_1", id, SUPERVISOR, |file| {
        assert_eq!(file, id);
        Err(Refusal::Audit)
    });
    assert_eq!(audited, Err(Refusal::Audit));
    assert_eq!(store.released("team_1", id, Some(SUPERVISOR)), Ok(false));
    for who in ["", &"x".repeat(129), "a\nb"] {
        assert_eq!(
            kind(acknowledge(&store, id, who)),
            StorageErrorKind::Input,
            "{who:?}"
        );
        assert_eq!(
            kind(store.released("team_1", id, Some(who))),
            StorageErrorKind::Input,
            "{who:?}"
        );
    }
    // A Team whose files are gone has nothing to acknowledge, and nothing is created for it.
    store.destroy("team_1").expect("destroyed");
    assert_eq!(
        kind(acknowledge(&store, id, SUPERVISOR)),
        StorageErrorKind::NotFound
    );
    assert!(!fixture.root.join("team_1").exists());
}
