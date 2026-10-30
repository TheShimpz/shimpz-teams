//! What the controller starts from: the host seams (the system clock and entropy, the Docker socket, the Brain runtime,
//! and the real publication resolution and artifact verification), its own container's hostname, and its private
//! stores, each in its own volume.

use std::path::Path;
use std::sync::Arc;

use team_action::journal::{Journal, Limits as JournalLimits};
use team_action::stored_input::StoredInputStore;
use team_core::clock::{Clock, SystemClock};
use team_core::entropy::Entropy;
use team_inference::client::HttpBrain;
use team_inference::config::InferenceConfigStore;
use team_install::cosign::Cosign;
use team_install::developers::DevelopersResolver;
use team_install::release::{RELEASE_PROXY_HOST, RELEASE_PROXY_PORT, ReleaseProxy};
use team_install::verifier::SigstoreVerifier;
use team_integrations::store::IntegrationStore;
use team_storage::files::{DEFAULT_LIMIT_BYTES, FileStore};

use super::{Startup, group, step};
use crate::browser::sign_ins::SignInStore;
use crate::chat::continuation_store::{ContinuationStore, MAX_CONTINUATIONS};
use crate::chat::limit::ChatLimitStore;
use crate::docker::daemon::{Daemon, SOCKET};
use crate::layout::StateLayout;
use crate::names::TeamNames;
use crate::routine::diagnostics::DiagnosticStore;
use crate::routine::store::RoutineStore;
use crate::seams::Seams;

/// The Brain runtime Team reaches when the Compose service names no other.
const BRAIN_URL: &str = "http://brain:8080";
/// The group of the Brain runtime token Team issues.
const BRAIN_TOKEN_GROUP: &str = "shimpzbrain-runtime-token";

/// The host seams: the system clock and entropy, the Docker socket, the Brain runtime, and the real publication
/// resolution and artifact verification through the release proxy with the shipping cosign.
pub(super) fn host_seams(
    layout: &StateLayout,
    entropy: &Arc<dyn Entropy>,
) -> Result<Seams, Startup> {
    let daemon = Arc::new(step(
        Daemon::connect(Path::new(SOCKET)),
        "the Docker daemon",
    )?);
    let brain_group = group(BRAIN_TOKEN_GROUP)?;
    step(
        team_inference::token::ensure(&layout.brain_token, brain_group, entropy.as_ref()),
        "the Brain runtime token",
    )?;
    let url = std::env::var("SHIMPZ_BRAIN_RUNTIME_URL").unwrap_or_else(|_| BRAIN_URL.to_owned());
    let brain = step(
        HttpBrain::new(&url, layout.brain_token.clone(), brain_group),
        "the Brain runtime URL",
    )?;
    let proxy = format!("http://{RELEASE_PROXY_HOST}:{RELEASE_PROXY_PORT}");
    Ok(Seams {
        clock: Arc::new(SystemClock),
        entropy: Arc::clone(entropy),
        docker: daemon.clone(),
        exec: daemon,
        brain: Arc::new(brain),
        resolver: Arc::new(DevelopersResolver::new(Arc::new(ReleaseProxy::team()))),
        verifier: Arc::new(SigstoreVerifier::new(
            Arc::new(ReleaseProxy::team()),
            Cosign::new(&layout.cosign, &proxy),
        )),
        #[cfg(feature = "testing")]
        any_resolution: false,
    })
}

/// The controller's own container: its hostname, which Docker sets to the container id.
pub(super) fn hostname() -> Result<String, Startup> {
    let raw = step(
        std::fs::read_to_string("/proc/sys/kernel/hostname"),
        "the hostname",
    )?;
    let name = raw.trim();
    if name.is_empty()
        || name.len() > 64
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    {
        return Err(Startup("the hostname"));
    }
    Ok(name.to_owned())
}

/// The private stores, each in its own volume.
pub(super) struct Stores {
    pub files: Arc<FileStore>,
    pub integrations: Arc<IntegrationStore>,
    pub stored_inputs: Arc<StoredInputStore>,
    pub continuations: Arc<ContinuationStore>,
    pub turns: Arc<crate::chat::turn::store::Turns>,
    pub inference: Arc<InferenceConfigStore>,
    pub names: TeamNames,
    pub limit: Arc<ChatLimitStore>,
    pub routines: Arc<RoutineStore>,
    pub diagnostics: Arc<DiagnosticStore>,
    pub sign_ins: Arc<SignInStore>,
    pub journal: Arc<Journal>,
}

/// The stores sealed under a keyring in their own volume: Integration credentials, Stored Inputs, paused turns, and
/// Sign-ins.
struct Sealed {
    integrations: IntegrationStore,
    stored_inputs: StoredInputStore,
    continuations: ContinuationStore,
    sign_ins: SignInStore,
}

fn sealed(
    layout: &StateLayout,
    clock: &Arc<dyn Clock>,
    entropy: &Arc<dyn Entropy>,
) -> Result<Sealed, Startup> {
    let key = |sealed: &crate::layout::Sealed| sealed.key.join("aes256.key");
    let integrations = IntegrationStore::new(
        layout.integrations.state.join("integrations.json"),
        key(&layout.integrations),
        clock.clone(),
        entropy.clone(),
    );
    let stored_inputs = StoredInputStore::new(
        layout.stored_inputs.state.join("stored-inputs.json"),
        key(&layout.stored_inputs),
        clock.clone(),
        entropy.clone(),
    );
    let continuations = ContinuationStore::new(
        &layout.chat_continuations.state.join("continuations.json"),
        &key(&layout.chat_continuations),
        clock.clone(),
        entropy.clone(),
        MAX_CONTINUATIONS,
    );
    let sign_ins = SignInStore::new(
        &layout.sign_ins.state,
        &key(&layout.sign_ins),
        clock.clone(),
        entropy.clone(),
    );
    Ok(Sealed {
        integrations: step(integrations, "the Integration store")?,
        stored_inputs: step(stored_inputs, "the Stored Input store")?,
        continuations: step(continuations, "the chat continuation store")?,
        sign_ins: step(sign_ins, "the Sign-in store")?,
    })
}

/// The Routine state and its diagnostics, each under its own key.
fn routine(
    layout: &StateLayout,
    entropy: &Arc<dyn Entropy>,
) -> Result<(RoutineStore, DiagnosticStore), Startup> {
    let routines = RoutineStore::new(
        &layout.routines.state,
        &layout.routines.key.join("aes256.key"),
        entropy.clone(),
    );
    let diagnostics = DiagnosticStore::new(
        &layout.routines.state.join("diagnostics"),
        &layout.routines.key.join("diagnostics.key"),
        entropy.clone(),
    );
    Ok((
        step(routines, "the Routine store")?,
        step(diagnostics, "the Routine diagnostics")?,
    ))
}

impl Stores {
    pub(super) fn open(
        layout: &StateLayout,
        clock: &Arc<dyn Clock>,
        entropy: &Arc<dyn Entropy>,
    ) -> Result<Self, Startup> {
        let files = FileStore::new(
            layout.storage.clone(),
            DEFAULT_LIMIT_BYTES,
            None,
            clock.clone(),
            entropy.clone(),
        );
        let journal = Journal::open(
            &layout.action_journal.join("journal.sqlite3"),
            JournalLimits::default(),
            entropy.clone(),
        );
        let sealed = sealed(layout, clock, entropy)?;
        let (routines, diagnostics) = routine(layout, entropy)?;
        Ok(Self {
            files: Arc::new(step(files, "the Team files")?),
            integrations: Arc::new(sealed.integrations),
            stored_inputs: Arc::new(sealed.stored_inputs),
            continuations: Arc::new(sealed.continuations),
            turns: Arc::new(crate::chat::turn::store::Turns::new(
                &layout.chat_turns,
                &layout.chat_continuations.key.join("aes256.key"),
                entropy.clone(),
            )),
            inference: Arc::new(InferenceConfigStore::new(layout.inference.clone())),
            names: TeamNames::new(layout.inference.clone()),
            limit: Arc::new(ChatLimitStore::new(&layout.inference)),
            routines: Arc::new(routines),
            diagnostics: Arc::new(diagnostics),
            sign_ins: Arc::new(sealed.sign_ins),
            journal: Arc::new(step(journal, "the Action journal")?),
        })
    }
}
