use std::sync::{Arc, Mutex};
use std::time::Duration;

use team_core::future::BoxFuture;
use team_core::secret::Secret;
use tokio::sync::Notify;

use super::super::{IntegrationStore, Revocation, Revoke, Status, StoreError};
use super::{ACCESS, ASSISTANT, REFRESH, account, fixture, metadata, put, resolve, scopes, tokens};
use crate::token::TokenSet;

fn lease() -> Secret {
    Secret::new("broker-lease-private-material-123456789".to_owned())
}

/// A refresh that announces it started, waits to be released, then answers `access`.
fn held(
    calls: Arc<Mutex<Vec<String>>>,
    entered: Arc<Notify>,
    release: Arc<Notify>,
    access: &'static str,
) -> impl FnOnce(Secret, Option<Secret>) -> BoxFuture<'static, Result<TokenSet, StoreError>> {
    move |refresh, broker_lease| {
        Box::pin(async move {
            calls
                .lock()
                .expect("usable")
                .push(refresh.expose().to_owned());
            assert_eq!(
                broker_lease.as_ref().map(Secret::expose),
                Some(lease().expose())
            );
            entered.notify_one();
            release.notified().await;
            Ok(tokens(access, Some(REFRESH), 3600))
        })
    }
}

async fn resolve_with(
    store: &Arc<IntegrationStore>,
    refresh: impl FnOnce(Secret, Option<Secret>) -> BoxFuture<'static, Result<TokenSet, StoreError>>,
) -> Result<Secret, StoreError> {
    store
        .resolve(
            ("team_1", ASSISTANT, "cloudflare"),
            ("cloudflare", &scopes()),
            refresh,
        )
        .await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn refresh_single_flight_60s() {
    let fixture = fixture();
    let expiring = TokenSet {
        broker_lease: Some(lease()),
        ..tokens(ACCESS, Some(REFRESH), 30)
    };
    put(&fixture.store, "team_1", "cloudflare", &expiring).expect("sealed");
    let status = || metadata(&fixture.store).expect("listed")[0].status;
    assert_eq!(status(), Status::Connected);
    fixture.clock.advance(Duration::from_secs(31));
    assert_eq!(status(), Status::RefreshRequired);

    let (calls, entered, release) = (
        Arc::default(),
        Arc::new(Notify::new()),
        Arc::new(Notify::new()),
    );
    let store = Arc::clone(&fixture.store);
    let first = tokio::spawn({
        let refresh = held(
            Arc::clone(&calls),
            Arc::clone(&entered),
            Arc::clone(&release),
            "refreshed-access-token-1",
        );
        async move { resolve_with(&store, refresh).await }
    });
    entered.notified().await;
    let store = Arc::clone(&fixture.store);
    let second = tokio::spawn(async move {
        resolve_with(&store, |_, _| {
            Box::pin(async { panic!("the second resolve reuses the first refresh") })
        })
        .await
    });
    release.notify_one();
    for task in [first, second] {
        assert_eq!(
            task.await.expect("joined").expect("resolved").expose(),
            "refreshed-access-token-1"
        );
    }
    assert_eq!(*calls.lock().expect("usable"), [REFRESH]);
    let row = metadata(&fixture.store).expect("listed");
    assert_eq!(
        (row[0].generation, &row[0].integration),
        (2, &Some(account()))
    );
    assert!(fixture.store.idle());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_hanging_refresh_never_blocks_another_integration() {
    let fixture = fixture();
    put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &TokenSet {
            broker_lease: Some(lease()),
            ..tokens(ACCESS, Some(REFRESH), 30)
        },
    )
    .expect("sealed");
    put(
        &fixture.store,
        "team_1",
        "secondary",
        &tokens("other-access-token-private-1", None, 3600),
    )
    .expect("sealed");
    fixture.clock.advance(Duration::from_secs(31));
    let (entered, release) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
    let store = Arc::clone(&fixture.store);
    let refreshing = tokio::spawn({
        let refresh = held(
            Arc::default(),
            Arc::clone(&entered),
            Arc::clone(&release),
            "refreshed-access-token-1",
        );
        async move { resolve_with(&store, refresh).await }
    });
    entered.notified().await;
    let other = tokio::time::timeout(
        Duration::from_millis(500),
        resolve(&fixture.store, "secondary"),
    )
    .await;
    release.notify_one();
    assert_eq!(
        other.expect("not blocked").expect("resolved").expose(),
        "other-access-token-private-1"
    );
    assert!(refreshing.await.expect("joined").is_ok());
    assert!(fixture.store.idle());
}

/// A revocation that announces it started and waits to be released.
struct HeldRevocation(Arc<Notify>, Arc<Notify>);

impl Revoke for HeldRevocation {
    fn revoke<'a>(&'a self, _grant: Revocation<'a>) -> BoxFuture<'a, Result<(), StoreError>> {
        Box::pin(async move {
            self.0.notify_one();
            self.1.notified().await;
            Ok(())
        })
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_hanging_revocation_never_blocks_another_integration() {
    let fixture = fixture();
    put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens(ACCESS, Some(REFRESH), 3600),
    )
    .expect("sealed");
    put(
        &fixture.store,
        "team_1",
        "secondary",
        &tokens("other-access-token-private-1", None, 3600),
    )
    .expect("sealed");
    let (entered, release) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
    let store = Arc::clone(&fixture.store);
    let revoke = HeldRevocation(Arc::clone(&entered), Arc::clone(&release));
    let replacing = tokio::spawn(async move {
        store
            .replace(
                ("team_1", ASSISTANT, "cloudflare"),
                ("cloudflare", &scopes()),
                None,
                || Box::pin(async { Ok(tokens("replacement-access-token-1", None, 3600)) }),
                &revoke,
            )
            .await
    });
    entered.notified().await;
    let other = tokio::time::timeout(
        Duration::from_millis(500),
        resolve(&fixture.store, "secondary"),
    )
    .await;
    release.notify_one();
    assert_eq!(
        other.expect("not blocked").expect("resolved").expose(),
        "other-access-token-private-1"
    );
    assert_eq!(
        replacing
            .await
            .expect("joined")
            .expect("replaced")
            .generation,
        3
    );
    assert!(fixture.store.idle());
}

#[tokio::test]
async fn a_refresh_detects_a_concurrent_generation_change() {
    let fixture = fixture();
    put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens(ACCESS, Some(REFRESH), 30),
    )
    .expect("sealed");
    fixture.clock.advance(Duration::from_secs(31));
    let store = Arc::clone(&fixture.store);
    let refreshed = resolve_with(&fixture.store, move |_, _| {
        put(
            &store,
            "team_1",
            "cloudflare",
            &tokens("concurrent-access-token-material", Some(REFRESH), 3600),
        )
        .expect("sealed");
        Box::pin(async {
            Ok(tokens(
                "refreshed-access-token-material",
                Some(REFRESH),
                3600,
            ))
        })
    })
    .await;
    assert_eq!(refreshed.err(), Some(StoreError::Reauthorization));
    assert_eq!(
        resolve(&fixture.store, "cloudflare")
            .await
            .expect("resolved")
            .expose(),
        "concurrent-access-token-material"
    );
}

#[tokio::test]
async fn a_claimed_lifetime_is_anchored_to_delivery_and_refreshed_within_the_margin() {
    for (lifetime, advance, first) in [
        (3499_i64, 3499 - 61, ACCESS),
        (45, 0, "rotated-access-private-1234"),
    ] {
        let fixture = fixture();
        put(
            &fixture.store,
            "team_1",
            "cloudflare",
            &tokens(ACCESS, Some(REFRESH), lifetime),
        )
        .expect("sealed");
        fixture.clock.advance(Duration::from_secs(advance));
        let rotate =
            |_: Secret, _: Option<Secret>| -> BoxFuture<'static, Result<TokenSet, StoreError>> {
                Box::pin(async { Ok(tokens("rotated-access-private-1234", Some(REFRESH), 3600)) })
            };
        assert_eq!(
            resolve_with(&fixture.store, rotate)
                .await
                .expect("resolved")
                .expose(),
            first
        );
        fixture.clock.advance(Duration::from_secs(1));
        assert_eq!(
            resolve_with(&fixture.store, rotate)
                .await
                .expect("resolved")
                .expose(),
            "rotated-access-private-1234"
        );
    }
}

#[tokio::test]
async fn a_failed_refresh_is_reported_and_seals_nothing() {
    let fixture = fixture();
    put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens(ACCESS, Some(REFRESH), 30),
    )
    .expect("sealed");
    fixture.clock.advance(Duration::from_secs(31));
    let failed = resolve_with(&fixture.store, |_, _| {
        Box::pin(async { Err(StoreError::Exchange) })
    })
    .await;
    assert_eq!(failed.err(), Some(StoreError::Exchange));
    let wrong_scopes = resolve_with(&fixture.store, |_, _| {
        Box::pin(async {
            Ok(TokenSet {
                scopes: vec!["dns.read".to_owned()],
                ..tokens("refreshed-access-token-1", None, 3600)
            })
        })
    })
    .await;
    assert_eq!(wrong_scopes.err(), Some(StoreError::Invalid));
    assert_eq!(metadata(&fixture.store).expect("listed")[0].generation, 1);
    assert_eq!(
        fixture
            .store
            .resolve(
                ("team_1", ASSISTANT, "missing"),
                ("cloudflare", &scopes()),
                |_, _| Box::pin(async { Err(StoreError::Exchange) })
            )
            .await
            .err(),
        Some(StoreError::Missing)
    );
}
