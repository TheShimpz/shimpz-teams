//! The service groups the controller serves, built over the shared seams and stores: the publication side and the
//! Team view, the Action executor, attachment preparation, the Browser, chat and Routines, the Assistants group, the
//! Teams group with every teardown participant, and the server over them all.

use std::path::Path;
use std::sync::{Arc, Mutex};

use team_action::provider::{Capacity, Transport};
use team_action::rpc::DockerSlots;
use team_action::runner::{Executor, Runner};
use team_core::audit::AuditSink;
use team_core::secret::Secret;
use team_egress::policy::PolicyStore;
use team_integrations::broker::{BrokerClient, FixedTransport};
use team_integrations::pkce::{PkceLimits, PkceStore};
use team_integrations::service::OAuthService;

use super::host::Stores;
use super::{Startup, actions, group, integrations, state, step, team, web};
use crate::assistant::containers::{self, Containers};
use crate::audit::AuditLog;
use crate::authority::Verifier;
use crate::browser::http::BrowserGroup;
use crate::browser::runtime::{BrowserRuntime, Host, own_browser_image};
use crate::browser::service::{BrowserService, ServiceParts};
use crate::browser::sign_in::{SessionParts, SignInSessions};
use crate::browser::teardown::{Browsers, SignIns};
use crate::chat::inference::InferenceService;
use crate::chat::service::{ChatParts, ChatService, IntegrationChallenges};
use crate::chat::teardown::{ContinuationsTeardown, ConversationTeardown, InferenceTeardown};
use crate::chat::usage::UsageBook;
use crate::coordination::Coordination;
use crate::coordination::residue::TeamTeardown;
use crate::docker::labels::Space;
use crate::http::activity::Activity;
use crate::http::controller::Controller;
use crate::http::server::{Limits, Server, ServerParts};
use crate::http::service::Services;
use crate::install::admission::ImageAdmission;
use crate::install::automatic::{self, AutomaticUpdates};
use crate::install::collector::SnapshotCollector;
use crate::install::registry::Registry;
use crate::install::snapshots::Stage;
use crate::install::teardown::PublicationTeardown;
use crate::install::{self as install_group, Assistants};
use crate::layout::StateLayout;
use crate::lifecycle::network::TeamNetworks;
use crate::lifecycle::participants::{AssistantTeardown, EgressTeardown, Removals};
use crate::lifecycle::{self as teams_group, Lifecycle};
use crate::prepare;
use crate::prepare::files::Files;
use crate::prepare::service::LocalPreparer;
use crate::prepare::store::Store;
use crate::routine::lifecycle::RoutinesTeardown;
use crate::routine::proposal::{ProposalBook, RoutineChat};
use crate::routine::protection::RunProtections;
use crate::routine::recorder::RecordingBook;
use crate::routine::replay::lost_check;
use crate::routine::service::{RoutineParts, RoutineService};
use crate::seams::Seams;

/// The group of the Assistant egress policies the egress proxy reads.
const EGRESS_POLICY_GROUP: &str = "shimpzassistant-egress-policy";

/// What every group shares.
pub(super) struct Shared {
    pub seams: Seams,
    pub layout: StateLayout,
    pub space: Space,
    pub cpuset: String,
    pub hostname: String,
    pub audit: Arc<AuditLog>,
    pub verifier: Arc<Verifier>,
    pub activity: Arc<Activity>,
    pub coordination: Coordination,
    pub stores: Stores,
}

/// The publication side: registry, admission, containers, networks, and the Team view over them.
struct Publications {
    registry: Arc<Registry>,
    admission: Arc<ImageAdmission>,
    containers: Arc<Containers>,
    networks: Arc<TeamNetworks>,
    team: Arc<team::Team>,
}

fn publications(shared: &Shared) -> Result<Publications, Startup> {
    let seams = &shared.seams;
    let registry = registry(&shared.layout.publications, seams);
    let admission = Arc::new(ImageAdmission::new(seams.docker.clone(), registry.clone()));
    let policies = PolicyStore::new(
        shared.layout.assistant_egress.clone(),
        group(EGRESS_POLICY_GROUP)?,
    );
    let containers = Arc::new(Containers::new(containers::Parts {
        docker: seams.docker.clone(),
        exec: seams.exec.clone(),
        admission: admission.clone(),
        policies,
        entropy: seams.entropy.clone(),
        space: shared.space.clone(),
        cpuset: shared.cpuset.clone(),
    }));
    let networks = Arc::new(TeamNetworks::new(
        seams.docker.clone(),
        shared.space.clone(),
    ));
    let team = Arc::new(team::Team {
        networks: networks.clone(),
        names: shared.stores.names.clone(),
        containers: containers.clone(),
        registry: registry.clone(),
        admission: admission.clone(),
    });
    Ok(Publications {
        registry,
        admission,
        containers,
        networks,
        team,
    })
}

#[cfg(feature = "testing")]
fn registry(root: &Path, seams: &Seams) -> Arc<Registry> {
    Arc::new(if seams.any_resolution {
        Registry::admitting_any_resolution(root)
    } else {
        Registry::new(root)
    })
}

#[cfg(not(feature = "testing"))]
fn registry(root: &Path, _seams: &Seams) -> Arc<Registry> {
    Arc::new(Registry::new(root))
}

/// The Action executor over the journal, the private stores, the brokered OAuth flow, and the Assistant egress.
struct Actions {
    runner: Arc<Runner>,
    running: Arc<actions::Running>,
    oauth: Arc<OAuthService>,
    pkce: Arc<PkceStore>,
}

fn action_runner(shared: &Shared, publications: &Publications) -> Result<Actions, Startup> {
    let (seams, stores) = (&shared.seams, &shared.stores);
    let pkce = Arc::new(step(
        PkceStore::new(PkceLimits::default(), seams.entropy.clone()),
        "the OAuth PKCE store",
    )?);
    let broker = BrokerClient::new(Box::new(FixedTransport::local()));
    let oauth = Arc::new(OAuthService::new(
        pkce.clone(),
        stores.integrations.clone(),
        Arc::new(broker),
    ));
    let running = Arc::new(actions::Running::new(publications.team.clone()));
    let runner = Arc::new(Runner {
        journal: stores.journal.clone(),
        stored_inputs: stores.stored_inputs.clone(),
        integrations: stores.integrations.clone(),
        oauth: oauth.clone(),
        engine: seams.exec.clone(),
        slots: Arc::new(DockerSlots::default()),
        capacity: Capacity::default(),
        transport: Arc::new(Transport::local()),
        audit: Arc::new(super::audit::Attributed(shared.audit.clone())),
        workloads: running.clone(),
        entropy: seams.entropy.clone(),
        blocked: Mutex::default(),
        files: stores.files.clone(),
        file_slot: team_action::files::FileSlot::default(),
    });
    Ok(Actions {
        runner,
        running,
        oauth,
        pkce,
    })
}

/// The Browser service and the Space's sign-in sessions.
async fn browser(
    shared: &Shared,
    team: &Arc<team::Team>,
) -> Result<(Arc<BrowserService>, Arc<SignInSessions>), Startup> {
    let seams = &shared.seams;
    let host = Host {
        docker: seams.docker.clone(),
        exec: seams.exec.clone(),
        entropy: seams.entropy.clone(),
    };
    let image = own_browser_image(seams.docker.as_ref(), &shared.hostname).await;
    let runtime = Arc::new(BrowserRuntime::new(
        host,
        shared.space.id(),
        &shared.cpuset,
        image,
    ));
    let service = Arc::new(step(
        BrowserService::new(ServiceParts {
            runtime,
            files: Arc::new(web::Downloads(shared.stores.files.clone())),
            audit: shared.audit.clone(),
            entropy: seams.entropy.clone(),
            runs: shared.layout.browser_runs.clone(),
        }),
        "the Browser task ledger",
    )?);
    let sessions = Arc::new(SignInSessions::new(SessionParts {
        service: service.clone(),
        store: shared.stores.sign_ins.clone(),
        incarnations: team.clone(),
        audit: shared.audit.clone(),
        entropy: seams.entropy.clone(),
    }));
    Ok((service, sessions))
}

/// Every running group, and what runs beside the server.
pub(super) struct Composed {
    pub server: Server,
    pub chat: ChatService,
    pub routines: RoutineService,
    pub sessions: Arc<SignInSessions>,
    pub updates: AutomaticUpdates,
    pub assistants: Arc<Assistants>,
    pub audit: Arc<AuditLog>,
}

/// Attachment preparation over the Team files: the preparer chat and the Files group share, and its store.
fn preparation(shared: &Shared) -> (Arc<LocalPreparer>, Store) {
    let seams = &shared.seams;
    let helpers = prepare::session::Helpers {
        docker: seams.docker.clone(),
        exec: seams.exec.clone(),
        entropy: seams.entropy.clone(),
        space_id: shared.space.id().to_owned(),
        controller: shared.hostname.clone(),
        cpuset: Some(shared.cpuset.clone()),
    };
    let store = Store(shared.stores.files.clone());
    (Arc::new(LocalPreparer::new(helpers, store.clone())), store)
}

/// The chat and Routine services, which share the Routine state, recordings, and cards.
struct Turns {
    chat: ChatService,
    routines: RoutineService,
    challenges: Arc<IntegrationChallenges>,
    usage: Arc<UsageBook>,
}

/// What a chat turn about a Routine runs with: the Routine state, its diagnostics, the recordings, the cards, and the
/// lost-runs check over the journal and the boot's protections.
fn routine_chat(shared: &Shared, protections: &Arc<RunProtections>) -> RoutineChat {
    let (seams, stores) = (&shared.seams, &shared.stores);
    RoutineChat {
        routines: stores.routines.clone(),
        diagnostics: stores.diagnostics.clone(),
        recordings: Arc::new(RecordingBook::new(
            seams.clock.clone(),
            seams.entropy.clone(),
        )),
        proposals: Arc::new(ProposalBook::new(seams.clock.clone())),
        lost: lost_check(stores.journal.clone(), protections.clone()),
        clock: seams.clock.clone(),
        entropy: seams.entropy.clone(),
    }
}

/// The chat service.
fn chat(
    shared: &Shared,
    (publications, actions): (&Publications, &Actions),
    (preparer, browser): (Arc<LocalPreparer>, Arc<BrowserService>),
    (routine, challenges, usage): (RoutineChat, Arc<IntegrationChallenges>, Arc<UsageBook>),
) -> Result<ChatService, Startup> {
    let (seams, stores) = (&shared.seams, &shared.stores);
    let parts = ChatParts {
        space_id: shared.space.id().to_owned(),
        coordination: shared.coordination.clone(),
        team: publications.team.clone(),
        brain: seams.brain.clone(),
        actions: Arc::new(Executor(actions.runner.clone())),
        preparer,
        files: stores.files.clone(),
        inference: stores.inference.clone(),
        limit: stores.limit.clone(),
        continuations: stores.continuations.clone(),
        integrations: stores.integrations.clone(),
        stored_inputs: stores.stored_inputs.clone(),
        integration_challenges: challenges,
        pkce: actions.pkce.clone(),
        browser: Some(browser),
        routine,
        usage,
        audit: shared.audit.clone(),
        clock: seams.clock.clone(),
        entropy: seams.entropy.clone(),
    };
    step(ChatService::new(parts), "the chat service")
}

/// The Routine service; its runs dispatch through the executor's observed batches.
fn routines(
    shared: &Shared,
    (publications, actions): (&Publications, &Actions),
    chat: &ChatService,
    protections: Arc<RunProtections>,
) -> Result<RoutineService, Startup> {
    let (seams, stores) = (&shared.seams, &shared.stores);
    let parts = RoutineParts {
        space_id: shared.space.id().to_owned(),
        coordination: shared.coordination.clone(),
        team: publications.team.clone(),
        actions: Arc::new(actions::RoutineBatches(Executor(actions.runner.clone()))),
        journal: stores.journal.clone(),
        workloads: actions.running.clone(),
        key: shared.verifier.clone(),
        store: stores.routines.clone(),
        diagnostics: stores.diagnostics.clone(),
        protections,
        chat: chat.clone(),
        audit: shared.audit.clone(),
        clock: seams.clock.clone(),
        entropy: seams.entropy.clone(),
    };
    step(RoutineService::new(parts), "the Routine service")
}

fn turns(
    shared: &Shared,
    built: (&Publications, &Actions),
    preparation: (Arc<LocalPreparer>, Arc<BrowserService>),
) -> Result<Turns, Startup> {
    let entropy = &shared.seams.entropy;
    let challenges = Arc::new(step(
        team_integrations::pending::default_store(entropy.clone()),
        "the Integration challenge store",
    )?);
    let protections = Arc::new(step(
        RunProtections::new(entropy.as_ref()),
        "the Routine protections",
    )?);
    let usage = Arc::new(UsageBook::default());
    let routine = routine_chat(shared, &protections);
    let chat = chat(
        shared,
        built,
        preparation,
        (routine, challenges.clone(), usage.clone()),
    )?;
    let routines = routines(shared, built, &chat, protections)?;
    Ok(Turns {
        chat,
        routines,
        challenges,
        usage,
    })
}

/// The Assistants group.
fn assistants(
    shared: &Shared,
    (publications, actions): (&Publications, &Actions),
    turns: &Turns,
) -> Arc<Assistants> {
    let (seams, stores) = (&shared.seams, &shared.stores);
    Arc::new(Assistants::new(install_group::Parts {
        coordination: shared.coordination.clone(),
        networks: publications.networks.clone(),
        containers: publications.containers.clone(),
        registry: publications.registry.clone(),
        admission: publications.admission.clone(),
        resolver: seams.resolver.clone(),
        verifier: seams.verifier.clone(),
        executor: Arc::new(Executor(actions.runner.clone())),
        private: Arc::new(state::Private {
            integrations: stores.integrations.clone(),
            stored_inputs: stores.stored_inputs.clone(),
            continuations: stores.continuations.clone(),
            challenges: turns.challenges.clone(),
        }),
        stage: Stage {
            docker: seams.docker.clone(),
            space: shared.space.clone(),
            entropy: seams.entropy.clone(),
        },
        audit: shared.audit.clone(),
    }))
}

/// The services' participants: the publications' egress policies and bindings, chat, the Browser, and Routines.
fn service_participants(
    shared: &Shared,
    publications: &Publications,
    (assistants, turns, removals): (&Arc<Assistants>, &Turns, &Removals),
    (browser, sessions): (&Arc<BrowserService>, &Arc<SignInSessions>),
) -> Vec<Arc<dyn TeamTeardown>> {
    let stores = &shared.stores;
    vec![
        Arc::new(EgressTeardown::new(
            publications.containers.clone(),
            publications.registry.clone(),
            removals.clone(),
        )),
        Arc::new(PublicationTeardown::new(
            assistants.clone(),
            removals.clone(),
        )),
        Arc::new(ContinuationsTeardown(turns.chat.clone())),
        Arc::new(ConversationTeardown {
            space_id: shared.space.id().to_owned(),
            brain: shared.seams.brain.clone(),
            team: publications.team.clone(),
        }),
        Arc::new(InferenceTeardown {
            store: stores.inference.clone(),
            limit: stores.limit.clone(),
        }),
        Arc::new(Browsers {
            service: browser.clone(),
            sessions: sessions.clone(),
        }),
        Arc::new(SignIns {
            store: stores.sign_ins.clone(),
        }),
        Arc::new(RoutinesTeardown(turns.routines.clone())),
    ]
}

/// The participants of the residue kinds no service owns: Action checkpoints, private state, and stored files.
fn state_participants(shared: &Shared, publications: &Publications) -> Vec<Arc<dyn TeamTeardown>> {
    let stores = &shared.stores;
    vec![
        Arc::new(state::ActionCheckpoints {
            journal: stores.journal.clone(),
            team: publications.team.clone(),
        }),
        Arc::new(state::PrivateTeardown {
            integrations: stores.integrations.clone(),
            stored_inputs: stores.stored_inputs.clone(),
        }),
        Arc::new(state::Storage(stores.files.clone())),
    ]
}

/// The Teams group: every teardown participant of the 17 residue kinds, each once, or the controller does not start.
fn lifecycle(
    shared: &Shared,
    publications: &Publications,
    (assistants, turns): (&Arc<Assistants>, &Turns),
    browser: (&Arc<BrowserService>, &Arc<SignInSessions>),
) -> Result<Lifecycle, Startup> {
    let docker = &shared.seams.docker;
    let removals = Removals::default();
    let mut participants = service_participants(
        shared,
        publications,
        (assistants, turns, &removals),
        browser,
    );
    participants.extend(state_participants(shared, publications));
    let assistants = AssistantTeardown::new(
        docker.clone(),
        publications.containers.clone(),
        publications.registry.clone(),
        shared.space.clone(),
        removals,
    );
    let parts = teams_group::Parts {
        coordination: shared.coordination.clone(),
        networks: (*publications.networks).clone(),
        names: shared.stores.names.clone(),
        assistants: Arc::new(assistants),
        helpers: Arc::new(crate::docker::helpers::Helpers::new(
            docker.clone(),
            shared.space.clone(),
        )),
        participants,
        storage: shared.layout.storage.clone(),
        pin: shared.verifier.clone(),
    };
    step(Lifecycle::new(parts), "the teardown participants")
}

/// Everything the service groups are built from.
struct Built {
    publications: Publications,
    actions: Actions,
    preparer: Arc<LocalPreparer>,
    store: Store,
    browser: Arc<BrowserService>,
    sessions: Arc<SignInSessions>,
    turns: Turns,
    assistants: Arc<Assistants>,
}

/// The groups that answer the HTTP operations.
fn services(shared: &Shared, built: &Built, lifecycle: Lifecycle) -> Services {
    let (seams, stores, team) = (&shared.seams, &shared.stores, &built.publications.team);
    let audit: Arc<dyn AuditSink> = shared.audit.clone();
    let controller = Controller {
        activity: shared.activity.clone(),
        verifier: shared.verifier.clone(),
        docker: seams.docker.clone(),
        audit: audit.clone(),
    };
    let files = Files::new(
        built.preparer.clone(),
        built.store.clone(),
        team.clone(),
        shared.coordination.clone(),
        audit,
    );
    let integrations = integrations::Integrations {
        team: team.clone(),
        locks: shared.coordination.locks.clone(),
        store: stores.integrations.clone(),
        stored_inputs: stores.stored_inputs.clone(),
        challenges: built.turns.challenges.clone(),
        oauth: built.actions.oauth.clone(),
        pkce: built.actions.pkce.clone(),
    };
    Services {
        controller: Arc::new(controller),
        teams: Arc::new(lifecycle),
        files: Arc::new(files),
        browser: Arc::new(BrowserGroup {
            service: built.browser.clone(),
            sessions: built.sessions.clone(),
        }),
        inference: Arc::new(InferenceService::new(
            team.clone(),
            stores.inference.clone(),
            shared.coordination.locks.clone(),
        )),
        chat: Arc::new(built.turns.chat.clone()),
        integrations: Arc::new(integrations),
        routines: Arc::new(built.turns.routines.clone()),
        assistants: built.assistants.clone(),
    }
}

/// The automatic updater over the Assistants group and the superseded snapshot collector.
fn updates(shared: &Shared, built: &Built) -> AutomaticUpdates {
    let seams = &shared.seams;
    let reconciled = automatic::Lifecycle {
        assistants: built.assistants.clone(),
        collector: Arc::new(SnapshotCollector::new(
            seams.docker.clone(),
            built.publications.registry.clone(),
        )),
    };
    AutomaticUpdates::new(
        Arc::new(reconciled),
        shared.audit.clone(),
        shared.activity.clone(),
        seams.entropy.clone(),
    )
}

async fn built(shared: &Shared) -> Result<Built, Startup> {
    let publications = publications(shared)?;
    let actions = action_runner(shared, &publications)?;
    let (preparer, store) = preparation(shared);
    let (browser, sessions) = browser(shared, &publications.team).await?;
    let turns = turns(
        shared,
        (&publications, &actions),
        (preparer.clone(), browser.clone()),
    )?;
    let assistants = assistants(shared, (&publications, &actions), &turns);
    Ok(Built {
        publications,
        actions,
        preparer,
        store,
        browser,
        sessions,
        turns,
        assistants,
    })
}

/// The controller over `shared`, serving with `token`.
pub(super) async fn compose(shared: Shared, token: Secret) -> Result<Composed, Startup> {
    let built = built(&shared).await?;
    let lifecycle = lifecycle(
        &shared,
        &built.publications,
        (&built.assistants, &built.turns),
        (&built.browser, &built.sessions),
    )?;
    let services = services(&shared, &built, lifecycle);
    let seams = &shared.seams;
    let server = Server::new(ServerParts {
        token,
        services,
        verifier: shared.verifier.clone(),
        challenges: crate::routine::service::http::challenges(
            built.turns.chat.clone(),
            built.turns.routines.clone(),
        ),
        activity: shared.activity.clone(),
        audit: shared.audit.clone(),
        usage: built.turns.usage.clone(),
        clock: seams.clock.clone(),
        entropy: seams.entropy.clone(),
        limits: Limits::default(),
    });
    Ok(Composed {
        server,
        updates: updates(&shared, &built),
        chat: built.turns.chat,
        routines: built.turns.routines,
        sessions: built.sessions,
        assistants: built.assistants,
        audit: shared.audit.clone(),
    })
}
