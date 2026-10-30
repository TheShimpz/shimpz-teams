use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use strict_json::Value;
use team_core::clock::Clock;
use team_core::entropy::{Entropy, EntropyUnavailable};
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::testing::{CountingEntropy, FakeClock};
use team_storage::private::StateError;

use super::{
    Declaration, Identity, IntegrationStore, Metadata, Revocation, Revoke, Status, StoreError,
};
use crate::token::TokenSet;

mod flights;
mod replacement;

pub(super) const ACCESS: &str = "access-token-private-material-123456789";
pub(super) const REFRESH: &str = "refresh-token-private-material-987654321";
pub(super) const ASSISTANT: &str = "shimpz-cloudflare";

pub(super) fn scopes() -> Vec<String> {
    ["dns.read", "offline_access", "zone.read"]
        .map(str::to_owned)
        .to_vec()
}

pub(super) fn declarations() -> Vec<(String, Declaration)> {
    vec![(
        "cloudflare".to_owned(),
        Declaration {
            provider: "cloudflare".to_owned(),
            scopes: scopes(),
        },
    )]
}

pub(super) fn account() -> Identity {
    Identity {
        id: "2244994945".to_owned(),
        username: Some("Cloudflare".to_owned()),
        name: Some("Cloudflare".to_owned()),
    }
}

pub(super) fn tokens(access: &str, refresh: Option<&str>, expires_in: i64) -> TokenSet {
    TokenSet {
        access_token: Secret::new(access.to_owned()),
        refresh_token: refresh.map(|refresh| Secret::new(refresh.to_owned())),
        scopes: scopes(),
        expires_in,
        broker_lease: None,
    }
}

pub(super) struct Fixture {
    pub root: tempfile::TempDir,
    pub clock: Arc<FakeClock>,
    pub store: Arc<IntegrationStore>,
}

pub(super) fn open(
    root: &Path,
    clock: Arc<FakeClock>,
    entropy: Arc<dyn Entropy>,
) -> Arc<IntegrationStore> {
    let clock: Arc<dyn Clock> = clock;
    Arc::new(
        IntegrationStore::new(
            root.join("state/integrations.json"),
            root.join("key/aes256.key"),
            clock,
            entropy,
        )
        .expect("separate paths"),
    )
}

pub(super) fn fixture() -> Fixture {
    let root = tempfile::tempdir().expect("temporary directory");
    let clock = Arc::new(FakeClock::at(
        UNIX_EPOCH + Duration::from_secs(1_000_000_000),
    ));
    let store = open(
        root.path(),
        Arc::clone(&clock),
        Arc::new(CountingEntropy::default()),
    );
    Fixture { root, clock, store }
}

pub(super) fn put(
    store: &IntegrationStore,
    team: &str,
    integration: &str,
    tokens: &TokenSet,
) -> Result<Metadata, StoreError> {
    store.put(
        (team, ASSISTANT, integration),
        ("cloudflare", &scopes()),
        tokens,
        Some(&account()),
    )
}

pub(super) async fn resolve(
    store: &Arc<IntegrationStore>,
    integration: &str,
) -> Result<Secret, StoreError> {
    store
        .resolve(
            ("team_1", ASSISTANT, integration),
            ("cloudflare", &scopes()),
            |_, _| Box::pin(async { panic!("an unexpired token never refreshes") }),
        )
        .await
}

pub(super) fn metadata(store: &IntegrationStore) -> Result<Vec<Metadata>, StoreError> {
    store.metadata("team_1", ASSISTANT, &declarations())
}

/// Fills each request with its next queued byte.
struct Nonces(Mutex<Vec<u8>>);

impl Entropy for Nonces {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        bytes.fill(self.0.lock().expect("usable").remove(0));
        Ok(())
    }
}

/// Records each revocation's access token.
#[derive(Default)]
pub(super) struct Revocations(pub Mutex<Vec<String>>);

impl Revoke for Revocations {
    fn revoke<'a>(&'a self, grant: Revocation<'a>) -> BoxFuture<'a, Result<(), StoreError>> {
        self.0
            .lock()
            .expect("usable")
            .push(grant.access_token.expose().to_owned());
        Box::pin(async { Ok(()) })
    }
}

/// Seals `access` (with a refresh token, for an hour) as Integration `integration` of `team`.
pub(super) fn sealed(
    store: &IntegrationStore,
    team: &str,
    integration: &str,
    access: &str,
) -> Metadata {
    put(
        store,
        team,
        integration,
        &tokens(access, Some(REFRESH), 3600),
    )
    .expect("sealed")
}

/// The access token `team_1`'s `integration` resolves to without a refresh.
pub(super) async fn access(store: &Arc<IntegrationStore>, integration: &str) -> String {
    resolve(store, integration)
        .await
        .expect("resolved")
        .expose()
        .to_owned()
}

fn state_path(fixture: &Fixture) -> std::path::PathBuf {
    fixture.root.path().join("state/integrations.json")
}

fn install_key(root: &Path) {
    fs::create_dir(root.join("key")).expect("created");
    fs::set_permissions(root.join("key"), fs::Permissions::from_mode(0o700)).expect("mode");
    fs::write(root.join("key/aes256.key"), (0..32).collect::<Vec<u8>>()).expect("written");
    fs::set_permissions(
        root.join("key/aes256.key"),
        fs::Permissions::from_mode(0o600),
    )
    .expect("mode");
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes.iter().fold(String::new(), |mut text, byte| {
        let _ = write!(text, "{byte:02x}");
        text
    })
}

#[tokio::test]
async fn sealed_state_bytes_stay_pinned_and_decode_after_reopen() {
    let fixture = fixture();
    let root = fixture.root.path();
    install_key(root);
    let store = open(
        root,
        Arc::clone(&fixture.clock),
        Arc::new(Nonces(Mutex::new(vec![1, 2]))),
    );
    sealed(&store, "team_1", "cloudflare", ACCESS);
    let other = ("team_2", "other", "cloudflare");
    store
        .put(
            other,
            ("cloudflare", &scopes()),
            &tokens(ACCESS, None, 3600),
            None,
        )
        .expect("sealed");
    // The bytes the Python writer produced for the same keyring, nonces, clock, and grants.
    let bytes = fs::read(state_path(&fixture)).expect("read");
    let pinned = "acca9649bea9651e61b2d674be2a1f1d8ba296e0a9070d8de1957d10bf0af364";
    assert_eq!(hex(&Sha256::digest(&bytes)), pinned);
    let reopened = open(root, fixture.clock, Arc::new(CountingEntropy::default()));
    assert_eq!(access(&reopened, "cloudflare").await, ACCESS);
    assert_eq!(
        metadata(&reopened).expect("listed")[0].integration,
        Some(account())
    );
}

#[test]
fn inventory_holds_missing_and_encrypted_rows() {
    let fixture = fixture();
    let missing = metadata(&fixture.store).expect("listed");
    let row = (
        missing[0].status,
        missing[0].generation,
        missing[0].expires_at,
    );
    assert_eq!(row, (Status::Missing, 0, None));
    let stored = sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    assert_eq!((stored.generation, stored.status), (1, Status::Connected));
    assert_eq!(stored.integration, Some(account()));
    assert_eq!(metadata(&fixture.store).expect("listed"), vec![stored]);
}

#[test]
fn no_token_or_identity_is_ever_stored_in_clear_or_printed() {
    let fixture = fixture();
    let stored = sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    let state = fs::read_to_string(state_path(&fixture)).expect("read");
    let key = fs::read(fixture.root.path().join("key/aes256.key")).expect("read");
    let leaked = [
        ACCESS,
        REFRESH,
        "2244994945",
        "Cloudflare",
        "access_token",
        "refresh_token",
    ]
    .iter()
    .filter(|private| {
        state.contains(*private)
            || key
                .windows(private.len())
                .any(|window| window == private.as_bytes())
    })
    .count();
    assert_eq!(leaked, 0);
    assert!(!format!("{stored:?}").contains(ACCESS));
    let modes: Vec<u32> = ["state/integrations.json", "key/aes256.key", "state", "key"]
        .iter()
        .map(|path| {
            fs::metadata(fixture.root.path().join(path))
                .expect("stat")
                .mode()
                & 0o7777
        })
        .collect();
    assert_eq!(modes, [0o600, 0o600, 0o700, 0o700]);
}

#[tokio::test]
async fn an_external_atomic_replacement_is_read_again() {
    let fixture = fixture();
    let second = open(
        fixture.root.path(),
        Arc::clone(&fixture.clock),
        Arc::new(CountingEntropy::default()),
    );
    sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    assert_eq!(access(&fixture.store, "cloudflare").await, ACCESS);
    let replacement = "replacement-access-token-private-material";
    sealed(&second, "team_1", "cloudflare", replacement);
    assert_eq!(access(&fixture.store, "cloudflare").await, replacement);
}

#[tokio::test]
async fn a_failed_write_changes_nothing_and_rotation_advances_the_generation() {
    let fixture = fixture();
    let first = sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    let state = fixture.root.path().join("state");
    fs::set_permissions(&state, fs::Permissions::from_mode(0o500)).expect("mode");
    let failed = put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens("replacement-access-1", None, 3600),
    );
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).expect("mode");
    assert!(matches!(failed, Err(StoreError::State(_))));
    assert_eq!(access(&fixture.store, "cloudflare").await, ACCESS);
    let second = sealed(
        &fixture.store,
        "team_1",
        "cloudflare",
        "new-access-token-123456789",
    );
    assert_eq!((first.generation, second.generation), (1, 2));
    assert_eq!(
        access(&fixture.store, "cloudflare").await,
        "new-access-token-123456789"
    );
}

fn reduced() -> Vec<String> {
    vec!["dns.read".to_owned(), "zone.read".to_owned()]
}

#[tokio::test]
async fn provider_or_scope_change_forces_reauthorization() {
    let fixture = fixture();
    let narrow = TokenSet {
        scopes: reduced(),
        ..tokens(ACCESS, None, 30)
    };
    let reference = ("team_1", ASSISTANT, "cloudflare");
    fixture
        .store
        .put(reference, ("cloudflare", &reduced()), &narrow, None)
        .expect("sealed");
    let drifted = metadata(&fixture.store).expect("listed");
    let row = (
        drifted[0].status,
        &drifted[0].scopes,
        &drifted[0].integration,
    );
    assert_eq!(row, (Status::ReauthorizationRequired, &scopes(), &None));
    assert_eq!(
        resolve(&fixture.store, "cloudflare").await.err(),
        Some(StoreError::Reauthorization)
    );
}

#[tokio::test]
async fn an_expired_grant_without_a_refresh_token_requires_reauthorization() {
    let fixture = fixture();
    let narrow = TokenSet {
        scopes: reduced(),
        ..tokens(ACCESS, None, 30)
    };
    let reference = ("team_1", ASSISTANT, "cloudflare");
    fixture
        .store
        .put(reference, ("cloudflare", &reduced()), &narrow, None)
        .expect("sealed");
    fixture.clock.advance(Duration::from_secs(31));
    let declaration = Declaration {
        provider: "cloudflare".to_owned(),
        scopes: reduced(),
    };
    let row = fixture.store.metadata(
        "team_1",
        ASSISTANT,
        &[("cloudflare".to_owned(), declaration)],
    );
    assert_eq!(
        row.expect("listed")[0].status,
        Status::ReauthorizationRequired
    );
    let refreshed = fixture
        .store
        .resolve(reference, ("cloudflare", &reduced()), |_, _| {
            Box::pin(async { panic!("no refresh token") })
        })
        .await;
    assert_eq!(refreshed.err(), Some(StoreError::Reauthorization));
}

fn rewrite(path: &Path, change: impl FnOnce(&mut Value)) {
    let mut state = strict_json::parse(&fs::read(path).expect("read")).expect("state");
    change(&mut state);
    let text = team_protocol::python::json::dumps(&state, team_storage::private::STATE_JSON);
    fs::write(path, text).expect("written");
}

fn member<'a>(value: &'a mut Value, key: &str) -> &'a mut Value {
    let Value::Object(members) = value else {
        panic!("an object");
    };
    &mut members
        .iter_mut()
        .find(|(name, _)| name == key)
        .expect("member")
        .1
}

fn record<'a>(state: &'a mut Value, team: &str) -> &'a mut Value {
    member(
        member(member(member(state, "teams"), team), ASSISTANT),
        "cloudflare",
    )
}

fn int(value: i64) -> Value {
    Value::Number(strict_json::Number::Int(value))
}

#[test]
fn a_record_copied_to_another_team_never_opens() {
    let fixture = fixture();
    sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    sealed(
        &fixture.store,
        "team_2",
        "cloudflare",
        "other-access-token-123456789",
    );
    rewrite(&state_path(&fixture), |state| {
        let copied = record(state, "team_1").clone();
        *record(state, "team_2") = copied;
    });
    let copied = fixture.store.metadata("team_2", ASSISTANT, &declarations());
    assert_eq!(
        copied.err(),
        Some(StoreError::State(StateError::Unauthentic))
    );
}

#[test]
fn envelope_roundtrip_and_tamper_each_aad_field() {
    let fixture = fixture();
    sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    let path = state_path(&fixture);
    let original = fs::read(&path).expect("read");
    let narrow = Value::Array(reduced().into_iter().map(Value::String).collect());
    let tampered: [(&str, Value); 4] = [
        ("expires_at", int(1_000_003_601)),
        (
            "status",
            Value::String("reauthorization-required".to_owned()),
        ),
        ("generation", int(2)),
        ("scopes", narrow),
    ];
    let refused = tampered.into_iter().filter(|(field, value)| {
        fs::write(&path, &original).expect("restored");
        rewrite(&path, |state| {
            *member(record(state, "team_1"), field) = value.clone();
        });
        matches!(metadata(&fixture.store), Err(StoreError::State(_)))
    });
    assert_eq!(refused.count(), 4);
}

#[test]
fn a_missing_or_substituted_key_fails_closed_without_replacement() {
    let fixture = fixture();
    sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    let key = fixture.root.path().join("key/aes256.key");
    let original = fs::read(state_path(&fixture)).expect("read");
    fs::remove_file(&key).expect("removed");
    let refused = put(
        &fixture.store,
        "team_1",
        "cloudflare",
        &tokens("replacement-token-12345", None, 3600),
    );
    assert!(matches!(refused, Err(StoreError::State(_))));
    assert!(!key.exists());
    assert_eq!(fs::read(state_path(&fixture)).expect("read"), original);
    fs::write(&key, [7_u8; 32]).expect("written");
    fs::set_permissions(&key, fs::Permissions::from_mode(0o600)).expect("mode");
    assert_eq!(
        metadata(&fixture.store).err(),
        Some(StoreError::State(StateError::Unauthentic))
    );
}

#[test]
fn invalid_token_sets_and_identities_change_nothing() {
    let fixture = fixture();
    sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    let original = fs::read(state_path(&fixture)).expect("read");
    let unreviewed = TokenSet {
        scopes: vec!["dm.read".to_owned()],
        ..tokens(ACCESS, None, 3600)
    };
    let invalid = [
        tokens("line\nbreak-token-123", None, 3600),
        unreviewed,
        tokens(ACCESS, None, 29),
    ];
    let refused = invalid.iter().filter(|tokens| {
        put(&fixture.store, "team_1", "cloudflare", tokens) == Err(StoreError::Invalid)
    });
    assert_eq!(refused.count(), 3);
    let wide = Identity {
        id: "x".repeat(257),
        username: None,
        name: None,
    };
    let reference = ("team_1", ASSISTANT, "cloudflare");
    let identity = fixture.store.put(
        reference,
        ("cloudflare", &scopes()),
        &tokens(ACCESS, None, 3600),
        Some(&wide),
    );
    assert_eq!(identity.err(), Some(StoreError::Invalid));
    assert_eq!(fs::read(state_path(&fixture)).expect("read"), original);
}

#[test]
fn unsafe_modes_links_and_duplicate_json_fail_closed() {
    let fixture = fixture();
    sealed(&fixture.store, "team_1", "cloudflare", ACCESS);
    let path = state_path(&fixture);
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).expect("mode");
    assert_eq!(
        metadata(&fixture.store).err(),
        Some(StoreError::State(StateError::Unsafe))
    );
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).expect("mode");
    fs::write(
        &path,
        br#"{"schema":1,"teams":{},"teams":{},"last_generation":0}"#,
    )
    .expect("written");
    assert_eq!(
        metadata(&fixture.store).err(),
        Some(StoreError::State(StateError::Malformed))
    );
    fs::remove_file(&path).expect("removed");
    let target = fixture.root.path().join("target.json");
    fs::write(&target, br#"{"schema":1,"teams":{},"last_generation":0}"#).expect("written");
    std::os::unix::fs::symlink(&target, &path).expect("linked");
    assert_eq!(
        metadata(&fixture.store).err(),
        Some(StoreError::State(StateError::Unsafe))
    );
}

fn status(store: &IntegrationStore, team: &str, assistant: &str) -> Status {
    store
        .metadata(team, assistant, &declarations())
        .expect("listed")[0]
        .status
}

#[test]
fn credentials_deleted_on_uninstall_and_teardown() {
    let fixture = fixture();
    let store = &fixture.store;
    for (team, assistant) in [
        ("team_1", "first"),
        ("team_1", "second"),
        ("team_2", "second"),
    ] {
        let declared = ("cloudflare", scopes());
        store
            .put(
                (team, assistant, "cloudflare"),
                (declared.0, &declared.1),
                &tokens(ACCESS, None, 3600),
                None,
            )
            .expect("sealed");
    }
    assert_eq!(
        store.retain_declared("team_1", "first", &["cloudflare"]),
        Ok(false)
    );
    assert_eq!(store.retain_declared("team_1", "first", &[]), Ok(true));
    assert_eq!(store.delete_assistant("team_1", "first"), Ok(false));
    assert_eq!(status(store, "team_1", "second"), Status::Connected);
    assert_eq!(
        (store.delete_team("team_1"), store.delete_team("team_1")),
        (Ok(true), Ok(false))
    );
    assert_eq!(
        (
            status(store, "team_1", "second"),
            status(store, "team_2", "second")
        ),
        (Status::Missing, Status::Connected)
    );
    assert_eq!(
        (store.delete_all(), store.delete_all()),
        (Ok(true), Ok(false))
    );
    assert_eq!(status(store, "team_2", "second"), Status::Missing);
}

#[test]
fn generations_never_repeat_after_any_deletion() {
    let fixture = fixture();
    let store = &fixture.store;
    let connect = || sealed(store, "team_1", "cloudflare", ACCESS).generation;
    let mut issued = vec![connect()];
    let deletions: [&dyn Fn() -> Result<bool, StoreError>; 4] = [
        &|| store.retain_declared("team_1", ASSISTANT, &[]),
        &|| store.delete_assistant("team_1", ASSISTANT),
        &|| store.delete_team("team_1"),
        &|| store.delete_all(),
    ];
    for delete in deletions {
        assert_eq!(delete(), Ok(true));
        issued.push(connect());
    }
    assert_eq!(issued, [1, 2, 3, 4, 5]);
    rewrite(&state_path(&fixture), |state| {
        *member(state, "last_generation") = int(4);
    });
    let reopened = open(
        fixture.root.path(),
        fixture.clock,
        Arc::new(CountingEntropy::default()),
    );
    assert_eq!(
        metadata(&reopened).err(),
        Some(StoreError::State(StateError::Malformed))
    );
}

#[test]
fn ids_and_declarations_are_refused_before_any_write() {
    let fixture = fixture();
    let store = &fixture.store;
    let ids = [
        ("../team", ASSISTANT, "cloudflare"),
        ("team_1", "Bad", "cloudflare"),
        ("team_1", ASSISTANT, "Bad"),
    ];
    let refused = ids.into_iter().filter(|reference| {
        store.put(
            *reference,
            ("cloudflare", &scopes()),
            &tokens(ACCESS, None, 3600),
            None,
        ) == Err(StoreError::Invalid)
    });
    assert_eq!(refused.count(), 3);
    let mut duplicate = declarations();
    duplicate.extend(declarations());
    let template = declarations().remove(0).1;
    let many: Vec<(String, Declaration)> = (0..17)
        .map(|index| (format!("integration-{index}"), template.clone()))
        .collect();
    assert_eq!(
        store.metadata("team_1", ASSISTANT, &duplicate).err(),
        Some(StoreError::Invalid)
    );
    assert_eq!(
        store.metadata("team_1", ASSISTANT, &many).err(),
        Some(StoreError::Invalid)
    );
    assert!(!state_path(&fixture).exists());
}

#[test]
fn an_assistant_holds_at_most_sixteen_integrations() {
    let fixture = fixture();
    for index in 0..16 {
        sealed(
            &fixture.store,
            "team_1",
            &format!("integration-{index}"),
            ACCESS,
        );
    }
    let extra = put(
        &fixture.store,
        "team_1",
        "integration-16",
        &tokens(ACCESS, None, 3600),
    );
    assert_eq!(extra.err(), Some(StoreError::Capacity));
    assert!(
        put(
            &fixture.store,
            "team_1",
            "integration-0",
            &tokens(ACCESS, None, 3600)
        )
        .is_ok()
    );
    assert_eq!(
        StoreError::from(StateError::Malformed),
        StoreError::State(StateError::Malformed)
    );
}
