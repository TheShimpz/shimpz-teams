use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::sync::{Arc, Mutex};

use team_core::future::BoxFuture;

use super::super::{
    Declaration, IntegrationStore, Metadata, Revocation, Revoke, Status, StoreError,
};
use super::{
    ACCESS, ASSISTANT, REFRESH, Revocations, account, fixture, put, resolve, scopes, tokens,
};
use crate::token::TokenSet;

fn widened() -> Vec<String> {
    ["dns.read", "dns.write", "offline_access", "zone.read"]
        .map(str::to_owned)
        .to_vec()
}

async fn replace(
    store: &Arc<IntegrationStore>,
    scopes: &[String],
    exchange: Result<TokenSet, StoreError>,
    revoke: &dyn Revoke,
) -> Result<Metadata, StoreError> {
    store
        .replace(
            ("team_1", ASSISTANT, "cloudflare"),
            ("cloudflare", scopes),
            Some(account()),
            move || Box::pin(async move { exchange }),
            revoke,
        )
        .await
}

/// Observes the stored status at each revocation, then answers `result`.
struct Observed {
    store: Arc<IntegrationStore>,
    seen: Mutex<Vec<(String, Option<String>, Status)>>,
    result: Result<(), StoreError>,
}

impl Revoke for Observed {
    fn revoke<'a>(&'a self, grant: Revocation<'a>) -> BoxFuture<'a, Result<(), StoreError>> {
        let declared = vec![(
            "cloudflare".to_owned(),
            Declaration {
                provider: "cloudflare".to_owned(),
                scopes: widened(),
            },
        )];
        let status = self
            .store
            .metadata("team_1", ASSISTANT, &declared)
            .expect("listed")[0]
            .status;
        self.seen.lock().expect("usable").push((
            grant.access_token.expose().to_owned(),
            grant.refresh_token.map(|token| token.expose().to_owned()),
            status,
        ));
        Box::pin(async move { self.result })
    }
}

#[tokio::test]
async fn replace_demotes_and_revokes_before_the_exchange_and_advances_the_generation() {
    let fixture = fixture();
    let first = put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens(ACCESS, Some(REFRESH), 3600),
    )
    .expect("sealed");
    let observed = Observed {
        store: Arc::clone(&fixture.store),
        seen: Mutex::default(),
        result: Ok(()),
    };
    let replacement = TokenSet {
        scopes: widened(),
        ..tokens("replacement-access-token-123", None, 3600)
    };
    let replaced = replace(&fixture.store, &widened(), Ok(replacement), &observed)
        .await
        .expect("replaced");
    assert_eq!(
        *observed.seen.lock().expect("usable"),
        [(
            ACCESS.to_owned(),
            Some(REFRESH.to_owned()),
            Status::ReauthorizationRequired
        )]
    );
    assert_eq!(
        (first.generation, replaced.generation, replaced.status),
        (1, 2, Status::Connected)
    );
    let declared = vec![(
        "cloudflare".to_owned(),
        Declaration {
            provider: "cloudflare".to_owned(),
            scopes: widened(),
        },
    )];
    assert_eq!(
        fixture
            .store
            .metadata("team_1", ASSISTANT, &declared)
            .expect("listed"),
        vec![replaced]
    );
}

#[tokio::test]
async fn a_failed_revocation_stops_before_the_exchange_and_leaves_the_reauthorization_marker() {
    let fixture = fixture();
    put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens(ACCESS, Some(REFRESH), 3600),
    )
    .expect("sealed");
    let observed = Observed {
        store: Arc::clone(&fixture.store),
        seen: Mutex::default(),
        result: Err(StoreError::Revocation),
    };
    let exchanged = Arc::new(Mutex::new(false));
    let flag = Arc::clone(&exchanged);
    let refused = fixture
        .store
        .replace(
            ("team_1", ASSISTANT, "cloudflare"),
            ("cloudflare", &scopes()),
            None,
            move || {
                *flag.lock().expect("usable") = true;
                Box::pin(async { Ok(tokens("replacement-access-token-123", None, 3600)) })
            },
            &observed,
        )
        .await;
    assert_eq!(refused.err(), Some(StoreError::Revocation));
    assert!(!*exchanged.lock().expect("usable"));
    let row = super::metadata(&fixture.store).expect("listed");
    assert_eq!(
        (row[0].status, row[0].generation),
        (Status::ReauthorizationRequired, 1)
    );
    assert_eq!(
        resolve(&fixture.store, "cloudflare").await.err(),
        Some(StoreError::Reauthorization)
    );
}

#[tokio::test]
async fn a_failed_exchange_is_retryable_and_a_failed_seal_revokes_the_new_grant() {
    let fixture = fixture();
    put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens(ACCESS, Some(REFRESH), 3600),
    )
    .expect("sealed");
    let revocations = Revocations::default();
    let failed = replace(
        &fixture.store,
        &scopes(),
        Err(StoreError::Exchange),
        &revocations,
    )
    .await;
    assert_eq!(failed.err(), Some(StoreError::Exchange));
    assert_eq!(*revocations.0.lock().expect("usable"), [ACCESS]);
    assert_eq!(
        super::metadata(&fixture.store).expect("listed")[0].status,
        Status::ReauthorizationRequired
    );

    let state = fixture.root.path().join("state");
    fs::set_permissions(&state, fs::Permissions::from_mode(0o500)).expect("mode");
    let unsealed = replace(
        &fixture.store,
        &scopes(),
        Ok(tokens("replacement-access-token-123", None, 3600)),
        &revocations,
    )
    .await;
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).expect("mode");
    assert!(matches!(unsealed, Err(StoreError::State(_))));
    // The demotion itself could not be written either, so only the unsealed replacement is revoked.
    assert_eq!(*revocations.0.lock().expect("usable"), [ACCESS]);

    let retried = replace(
        &fixture.store,
        &scopes(),
        Ok(tokens("replacement-access-token-123", None, 3600)),
        &revocations,
    )
    .await;
    assert_eq!(retried.expect("replaced").status, Status::Connected);
    assert_eq!(*revocations.0.lock().expect("usable"), [ACCESS, ACCESS]);
}

#[tokio::test]
async fn a_seal_that_fails_after_the_exchange_revokes_the_unsealed_replacement() {
    let fixture = fixture();
    let revocations = Revocations::default();
    // A full Assistant refuses the new Integration only when it is sealed, after the exchange returned it.
    for index in 0..16 {
        put(
            &fixture.store,
            "team_1",
            &format!("integration-{index}"),
            &tokens(ACCESS, None, 3600),
        )
        .expect("sealed");
    }
    let refused = fixture
        .store
        .replace(
            ("team_1", ASSISTANT, "cloudflare"),
            ("cloudflare", &scopes()),
            None,
            || Box::pin(async { Ok(tokens("replacement-access-token-123", None, 3600)) }),
            &revocations,
        )
        .await;
    assert_eq!(refused.err(), Some(StoreError::Capacity));
    assert_eq!(
        *revocations.0.lock().expect("usable"),
        ["replacement-access-token-123"]
    );
    let invalid = replace(
        &fixture.store,
        &scopes(),
        Ok(tokens("line\nbreak", None, 3600)),
        &revocations,
    )
    .await;
    assert_eq!(invalid.err(), Some(StoreError::Invalid));
    assert_eq!(revocations.0.lock().expect("usable").len(), 1);
}
