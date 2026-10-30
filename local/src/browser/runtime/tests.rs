use std::collections::HashMap;
use std::time::Duration;

use bollard::models::{HealthStatusEnum, HealthcheckResult};
use team_core::stop::Stop;
use tokio::time::Instant;

use super::{Acquire, BrowserError, BrowserRuntime, Capsule, Lease, Mode, browser_image};
use crate::browser::fake::{EGRESS_ID, Fixture, generation, image, published};

fn agent(team_id: &str) -> Acquire<'_> {
    Acquire {
        team_id,
        language: "en",
        mode: Mode::Agent,
        capsule: None,
        deadline: None,
        stop: None,
    }
}

fn capsule(chunks: &[&[u8]]) -> Capsule {
    let chunks: Vec<_> = chunks.iter().map(|chunk| Ok(chunk.to_vec())).collect();
    Box::new(chunks.into_iter())
}

fn env(fixture: &Fixture) -> HashMap<String, String> {
    let state = fixture.daemon.state();
    let created = state.created.last().expect("a Browser was created");
    created
        .env
        .as_deref()
        .unwrap_or_default()
        .iter()
        .filter_map(|entry| entry.split_once('='))
        .map(|(name, value)| (name.to_owned(), value.to_owned()))
        .collect()
}

#[tokio::test(start_paused = true)]
async fn the_first_task_cleans_then_creates_one_hardened_browser_on_a_fresh_generation() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime
        .acquire(Acquire {
            language: "pt-BR",
            ..agent("team_1")
        })
        .await
        .expect("acquired");
    assert_eq!(fixture.daemon.state().restarts, 1);
    assert_eq!(lease.container_id, "browser-1");
    assert_eq!(lease.address, "172.30.0.9");
    assert_eq!(lease.egress.generation, generation(1));
    assert_eq!(lease.egress.container_id, EGRESS_ID);
    assert!(lease.credentials.is_none());
    assert_eq!(
        env(&fixture),
        HashMap::from([
            ("BROWSER_PROXY".to_owned(), "172.30.0.2".to_owned()),
            ("BROWSER_LANG".to_owned(), "pt-BR".to_owned()),
            ("BROWSER_MODE".to_owned(), "agent".to_owned()),
        ])
    );
    {
        let state = fixture.daemon.state();
        let created = &state.created[0];
        let labels = created.labels.as_ref().expect("labels");
        assert_eq!(labels["com.shimpz.local.kind"], "browser");
        assert_eq!(labels["com.shimpz.local.team-id"], "team_1");
        assert_eq!(labels["com.shimpz.local.image"], image());
    }
    assert!(
        runtime
            .container_name("team_1")
            .starts_with("shimpz-local-")
    );
    assert!(
        runtime
            .container_name("team_1")
            .ends_with("-team_1-browser")
    );
    runtime.release(&lease).await.expect("released");
    assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
    assert_eq!(fixture.daemon.state().restarts, 2);
    assert!(!runtime.dirty());
}

#[tokio::test(start_paused = true)]
async fn a_person_signs_in_on_a_controlled_browser_whose_display_holds_drawn_credentials() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime
        .acquire(Acquire {
            mode: Mode::Person("https://www.linkedin.com/login".to_owned()),
            ..agent("neo")
        })
        .await
        .expect("acquired");
    let environment = env(&fixture);
    assert_eq!(environment["BROWSER_MODE"], "person");
    assert_eq!(environment["BROWSER_URL"], "https://www.linkedin.com/login");
    assert!(!environment.contains_key("BROWSER_RESTORE"));
    assert_eq!(lease.mode, "person");
    let credentials = lease.credentials.clone().expect("credentials");
    assert_eq!(fixture.execs.input(&["credentials"]), credentials.file());
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn a_capsule_is_restored_before_chromium_is_ready() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime
        .acquire(Acquire {
            mode: Mode::SignedIn,
            capsule: Some(capsule(&[b"first", b"second"])),
            ..agent("neo")
        })
        .await
        .expect("acquired");
    assert_eq!(fixture.execs.input(&["profile", "import"]), b"firstsecond");
    assert!(lease.credentials.is_some());
    assert!(!env(&fixture).contains_key("BROWSER_URL"));
    runtime.release(&lease).await.expect("released");
    let lease = runtime
        .acquire(Acquire {
            mode: Mode::Person("https://example.com".to_owned()),
            capsule: Some(capsule(&[b"one"])),
            ..agent("neo")
        })
        .await
        .expect("acquired");
    assert_eq!(env(&fixture)["BROWSER_RESTORE"], "1");
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn only_a_mode_with_exactly_its_own_inputs_is_created() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let refused = [
        Acquire {
            mode: Mode::SignedIn,
            ..agent("neo")
        },
        Acquire {
            capsule: Some(capsule(&[])),
            ..agent("neo")
        },
    ];
    for request in refused {
        assert_eq!(
            runtime.acquire(request).await.map(|_| ()),
            Err(BrowserError::UNAVAILABLE)
        );
    }
    for language in ["", "pt_BR", "EN", "en-us", "--lang"] {
        assert_eq!(
            runtime
                .acquire(Acquire {
                    language,
                    ..agent("neo")
                })
                .await
                .map(|_| ()),
            Err(BrowserError::UNAVAILABLE)
        );
    }
    assert_eq!(fixture.daemon.state().created.len(), 0);
}

#[tokio::test(start_paused = true)]
async fn an_unavailable_browser_refuses_before_any_docker_call() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(None);
    assert!(!runtime.available());
    assert_eq!(
        runtime.acquire(agent("team_1")).await.map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
    assert_eq!(fixture.daemon.state().restarts, 0);
    // Where no Browser can run, no task has an egress generation: a cleanup only proves none is left.
    fixture.daemon.state().fail.insert("restart");
    runtime.clean().await.expect("cleaned");
    assert_eq!(fixture.daemon.state().restarts, 0);
}

#[tokio::test(start_paused = true)]
async fn a_restore_or_credentials_the_browser_refuses_removes_it() {
    for program in [&["credentials"][..], &["profile", "import"][..]] {
        let fixture = Fixture::new();
        fixture.execs.script(program, b"", 1);
        let runtime = fixture.runtime(Some(image()));
        let outcome = runtime
            .acquire(Acquire {
                mode: Mode::SignedIn,
                capsule: Some(capsule(&[b"capsule"])),
                ..agent("neo")
            })
            .await;
        assert_eq!(outcome.map(|_| ()), Err(BrowserError::UNAVAILABLE));
        assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
    }
    // A capsule chunk that cannot be read is no restore either.
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let unreadable: Capsule = Box::new(std::iter::once(Err(super::CapsuleUnreadable)));
    let outcome = runtime
        .acquire(Acquire {
            mode: Mode::SignedIn,
            capsule: Some(unreadable),
            ..agent("neo")
        })
        .await;
    assert_eq!(outcome.map(|_| ()), Err(BrowserError::UNAVAILABLE));
    assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
}

#[tokio::test(start_paused = true)]
async fn recovery_cleans_a_dirty_runtime_unless_a_task_holds_the_browser() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    // Dirty after a Team start: a failing cleanup proves nothing; one that works proves the Space clean.
    fixture.daemon.state().fail.insert("list");
    assert!(!runtime.recover().await);
    fixture.daemon.state().fail.clear();
    assert!(runtime.recover().await);
    assert!(!runtime.dirty());
    let restarts = fixture.daemon.state().restarts;
    assert!(runtime.recover().await);
    assert_eq!(fixture.daemon.state().restarts, restarts);
    let lease = runtime.acquire(agent("neo")).await.expect("acquired");
    // The holder cleaned first: nothing is left to recover until its own release says otherwise.
    assert!(runtime.recover().await);
    fixture.daemon.state().fail.insert("survive");
    assert_eq!(runtime.release(&lease).await, Err(BrowserError::LOST));
    assert!(runtime.dirty());
    fixture.daemon.state().fail.clear();
    assert!(runtime.recover().await);
    assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
}

async fn person(fixture: &Fixture) -> (BrowserRuntime, Lease) {
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime
        .acquire(Acquire {
            mode: Mode::Person("https://example.com".to_owned()),
            ..agent("neo")
        })
        .await
        .expect("acquired");
    (runtime, lease)
}

#[tokio::test(start_paused = true)]
async fn a_clean_stop_keeps_the_browser_and_any_doubt_is_a_lost_browser() {
    let fixture = Fixture::new();
    let (runtime, lease) = person(&fixture).await;
    fixture.execs.script(&["stop"], b"stopped\n", 0);
    assert_eq!(runtime.stop(&lease).await, Ok(()));
    for (answer, status) in [
        (&b"stopped\n"[..], 1),
        (b"", 0),
        (b"stopped\nmore\n", 0),
        (b"running\n", 0),
    ] {
        fixture.execs.script(&["stop"], answer, status);
        assert_eq!(runtime.stop(&lease).await, Err(BrowserError::LOST));
    }
    // An inspection that never answers cannot hold the stop past its own deadline.
    fixture.execs.script(&["stop"], b"stopped\n", 0);
    fixture.execs.state().stall.insert("inspect");
    let started = Instant::now();
    assert_eq!(runtime.stop(&lease).await, Err(BrowserError::LOST));
    assert!(started.elapsed() <= super::programs::STOP_SECONDS);
    fixture.execs.state().stall.clear();
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn an_export_streams_the_capsule_and_refuses_a_failed_one() {
    let fixture = Fixture::new();
    let (runtime, lease) = person(&fixture).await;
    fixture
        .execs
        .script(&["profile", "export"], &vec![b'x'; 3000], 0);
    let mut chunks = Vec::new();
    let total = runtime
        .export(&lease, &mut |chunk: &[u8]| {
            chunks.extend_from_slice(chunk);
            Ok(())
        })
        .await;
    assert_eq!(total, Ok(3000));
    assert_eq!(chunks, vec![b'x'; 3000]);
    fixture.execs.script(&["profile", "export"], b"partial", 1);
    assert_eq!(
        runtime.export(&lease, &mut |_: &[u8]| Ok(())).await,
        Err(BrowserError::LOST)
    );
    fixture
        .execs
        .script(&["profile", "export"], &vec![b'x'; 3000], 0);
    assert_eq!(
        runtime.export(&lease, &mut |_: &[u8]| Err(())).await,
        Err(BrowserError::LOST)
    );
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn a_second_task_never_shares_the_generation_an_earlier_one_used() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let first = runtime.acquire(agent("team_1")).await.expect("acquired");
    runtime.release(&first).await.expect("released");
    let second = runtime.acquire(agent("team_2")).await.expect("acquired");
    assert_ne!(first.egress, second.egress);
    assert_eq!(fixture.daemon.state().restarts, 2);
    runtime.release(&second).await.expect("released");
    // A generation an earlier task used, still running, is restarted before the next task.
    *runtime.used.lock().expect("usable") = Some(second.egress.clone());
    fixture.daemon.state().restarts = 2;
    let third = runtime.acquire(agent("team_1")).await.expect("acquired");
    assert_eq!(third.egress.generation, generation(3));
    runtime.release(&third).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn a_restart_waits_for_the_new_process_s_own_probe() {
    let fixture = Fixture::new();
    fixture.daemon.state().probe_lag = 3;
    let runtime = fixture.runtime(Some(image()));
    let started = Instant::now();
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    assert_eq!(lease.egress.generation, generation(1));
    assert!(started.elapsed() > Duration::ZERO);
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn a_lease_is_intact_only_on_its_own_running_browser_and_egress_process() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    assert!(runtime.intact(&lease).await);
    fixture.daemon.state().restarts += 1;
    assert!(!runtime.intact(&lease).await);
    runtime.release(&lease).await.expect("released");
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.daemon.halt(&lease.container_id);
    assert!(!runtime.intact(&lease).await);
    runtime.kill(&lease).await.expect("killed");
    assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
    assert!(!runtime.intact(&lease).await);
    runtime.release(&lease).await.expect("released");
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.daemon.state().fail.insert("egress");
    assert!(!runtime.intact(&lease).await);
}

#[tokio::test(start_paused = true)]
async fn a_probe_that_failed_since_leaves_the_lease_on_its_egress_process() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.daemon.state().health_log = Some(vec![
        HealthcheckResult {
            exit_code: Some(0),
            output: Some(format!("{}\n", generation(1))),
            ..HealthcheckResult::default()
        },
        HealthcheckResult {
            exit_code: Some(-1),
            output: Some("Health check exceeded timeout (3s)".to_owned()),
            ..HealthcheckResult::default()
        },
    ]);
    assert!(runtime.intact(&lease).await);
    fixture.daemon.state().health = Some(HealthStatusEnum::UNHEALTHY);
    assert!(!runtime.intact(&lease).await);
}

#[tokio::test(start_paused = true)]
async fn one_browser_per_space_and_anything_else_on_its_network_is_busy() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    assert_eq!(
        runtime.acquire(agent("team_2")).await.map(|_| ()),
        Err(BrowserError::BUSY)
    );
    // A Team that crashed without releasing: its Browser survives and refuses every Team until cleaned.
    runtime.free_slot();
    runtime
        .dirty
        .store(false, std::sync::atomic::Ordering::SeqCst);
    assert_eq!(
        runtime.acquire(agent("team_2")).await.map(|_| ()),
        Err(BrowserError::BUSY)
    );
    let next = runtime
        .acquire(agent("team_2"))
        .await
        .expect("cleaned first");
    assert_eq!(next.team_id, "team_2");
    runtime.release(&next).await.expect("released");
    drop(lease);
    fixture
        .daemon
        .state()
        .extra_members
        .push("stranger".to_owned());
    assert_eq!(
        runtime.acquire(agent("team_1")).await.map(|_| ()),
        Err(BrowserError::BUSY)
    );
}

type Break = fn(&Fixture);

fn breaks() -> Vec<(&'static str, Break)> {
    vec![
        ("internal", |f| {
            f.daemon.state().network.internal = Some(false);
        }),
        ("ipv6", |f| {
            f.daemon.state().network.enable_ipv6 = Some(true);
        }),
        ("gateway", |f| f.daemon.state().network.options = None),
        ("missing network", |f| {
            f.daemon.state().fail.insert("network");
        }),
        ("egress labels", |f| {
            f.daemon.state().egress_space = Some("other".to_owned());
        }),
        ("egress gone", |f| {
            f.daemon.state().fail.insert("egress");
        }),
        ("unhealthy", |f| {
            f.daemon.state().health = Some(HealthStatusEnum::STARTING);
        }),
        ("restart", |f| {
            f.daemon.state().fail.insert("restart");
        }),
        ("list", |f| {
            f.daemon.state().fail.insert("list");
        }),
        ("egress address", |f| {
            f.daemon.state().egress_unaddressed = true;
        }),
        ("probe missing", |f| {
            f.daemon.state().health_log = Some(Vec::new());
        }),
        ("probe failed", |f| {
            f.daemon.state().health_log = Some(vec![HealthcheckResult {
                exit_code: Some(1),
                output: Some(format!("{}\n", generation(0))),
                ..HealthcheckResult::default()
            }]);
        }),
        ("probe output", |f| {
            f.daemon.state().health_log = Some(vec![HealthcheckResult {
                exit_code: Some(0),
                output: Some("healthy\n".to_owned()),
                ..HealthcheckResult::default()
            }]);
        }),
        ("stale probe", |f| f.daemon.state().probe_lag = 1_000_000),
    ]
}

#[tokio::test(start_paused = true)]
async fn the_network_and_egress_must_be_exactly_the_owned_isolated_ones() {
    for (name, broken) in breaks() {
        let fixture = Fixture::new();
        broken(&fixture);
        let runtime = fixture.runtime(Some(image()));
        assert_eq!(
            runtime.acquire(agent("team_1")).await.map(|_| ()),
            Err(BrowserError::UNAVAILABLE),
            "{name}"
        );
        assert!(runtime.dirty(), "{name}");
        assert!(runtime.take_slot(), "{name}");
        assert_eq!(fixture.daemon.state().created.len(), 0, "{name}");
    }
}

#[tokio::test(start_paused = true)]
async fn a_published_image_is_pulled_by_digest_and_a_developer_image_never_is() {
    let fixture = Fixture::new();
    fixture.daemon.state().images.remove(&published());
    let runtime = fixture.runtime(Some(published()));
    let lease = runtime.acquire(agent("team_1")).await.expect("pulled");
    runtime.release(&lease).await.expect("released");
    assert_eq!(fixture.daemon.state().pulled, [published()]);
    let fixture = Fixture::new();
    fixture.daemon.state().images.remove(&image());
    let runtime = fixture.runtime(Some(image()));
    assert_eq!(
        runtime.acquire(agent("team_1")).await.map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
    assert_eq!(fixture.daemon.state().pulled.len(), 0);
    for failure in ["pull", "image"] {
        let fixture = Fixture::new();
        fixture.daemon.state().images.remove(&published());
        fixture.daemon.state().fail.insert(failure);
        let runtime = fixture.runtime(Some(published()));
        assert_eq!(
            runtime.acquire(agent("team_1")).await.map(|_| ()),
            Err(BrowserError::UNAVAILABLE)
        );
    }
}

#[tokio::test(start_paused = true)]
async fn a_pull_is_waited_for_only_while_the_task_may_wait() {
    let fixture = Fixture::new();
    fixture.daemon.state().images.remove(&published());
    fixture.daemon.state().stall.insert("pull");
    let runtime = fixture.runtime(Some(published()));
    let stop = Stop::new();
    let stopper = stop.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(5)).await;
        stopper.stop();
    });
    assert_eq!(
        runtime
            .acquire(Acquire {
                stop: Some(&stop),
                ..agent("team_1")
            })
            .await
            .map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
    assert_eq!(
        runtime
            .acquire(Acquire {
                deadline: Some(Instant::now() + Duration::from_secs(10)),
                ..agent("team_1")
            })
            .await
            .map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
    // A task that may no longer wait creates no Browser.
    assert_eq!(
        runtime
            .acquire(Acquire {
                deadline: Some(Instant::now()),
                ..agent("team_1")
            })
            .await
            .map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
    assert_eq!(fixture.daemon.state().created.len(), 0);
}

fn drifts() -> Vec<(&'static str, Break)> {
    vec![
        ("image mismatch", |f| {
            f.daemon.state().drift = Some(|c| c.image = Some("sha256:other".to_owned()));
        }),
        ("capability", |f| {
            f.daemon.state().drift = Some(|c| {
                c.host_config.as_mut().expect("host").cap_add = Some(vec!["NET_ADMIN".to_owned()]);
            });
        }),
        ("seccomp", |f| {
            f.daemon.state().drift = Some(|c| {
                c.host_config.as_mut().expect("host").security_opt = Some(vec![
                    "no-new-privileges:true".to_owned(),
                    "seccomp=unconfined".to_owned(),
                ]);
            });
        }),
        ("not ready", |f| f.execs.script(&["ready"], b"", 1)),
        ("second network", |f| {
            f.daemon.state().on_start = Some(|c| {
                c.network_settings
                    .as_mut()
                    .and_then(|settings| settings.networks.as_mut())
                    .expect("networks")
                    .insert(
                        "other".to_owned(),
                        bollard::models::EndpointSettings::default(),
                    );
            });
        }),
        ("stranger joined", |f| {
            f.daemon.state().strangers = vec!["stranger".to_owned()];
        }),
        ("start", |f| {
            f.daemon.state().fail.insert("start");
        }),
    ]
}

#[tokio::test(start_paused = true)]
async fn browser_drift_refused_and_removed() {
    for (name, cause) in drifts() {
        let fixture = Fixture::new();
        cause(&fixture);
        let runtime = fixture.runtime(Some(image()));
        assert_eq!(
            runtime.acquire(agent("team_1")).await.map(|_| ()),
            Err(BrowserError::UNAVAILABLE),
            "{name}"
        );
        assert_eq!(fixture.daemon.browsers(), Vec::<String>::new(), "{name}");
        let started = fixture
            .execs
            .state()
            .execs
            .iter()
            .any(|(_, arguments, _)| arguments == &["credentials"]);
        assert!(!started, "{name}");
    }
    let fixture = Fixture::new();
    fixture.daemon.state().fail.insert("create");
    let runtime = fixture.runtime(Some(image()));
    assert_eq!(
        runtime.acquire(agent("team_1")).await.map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
}

#[tokio::test(start_paused = true)]
async fn a_drifted_browser_is_never_started() {
    let fixture = Fixture::new();
    fixture.daemon.state().drift = Some(|c| {
        c.host_config.as_mut().expect("host").privileged = Some(true);
    });
    let runtime = fixture.runtime(Some(image()));
    assert_eq!(
        runtime.acquire(agent("team_1")).await.map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
    // Removed before any start: Docker never ran it and nothing ran inside it.
    assert_eq!(fixture.daemon.state().removed, ["browser-1"]);
    assert_eq!(fixture.execs.state().execs.len(), 0);
}

#[tokio::test(start_paused = true)]
async fn a_stop_while_the_browser_is_created_never_starts_chromium() {
    let fixture = Fixture::new();
    let stop = Stop::new();
    stop.stop();
    let runtime = fixture.runtime(Some(image()));
    assert_eq!(
        runtime
            .acquire(Acquire {
                stop: Some(&stop),
                ..agent("team_1")
            })
            .await
            .map(|_| ()),
        Err(BrowserError::UNAVAILABLE)
    );
    assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
    assert_eq!(fixture.execs.state().execs.len(), 0);
}

#[tokio::test(start_paused = true)]
async fn a_removal_is_proved_or_the_slot_stays_dirty() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.daemon.state().fail.insert("survive");
    assert_eq!(runtime.release(&lease).await, Err(BrowserError::LOST));
    assert!(runtime.dirty());
    fixture.daemon.state().fail = ["remove"].into();
    assert_eq!(runtime.clean().await, Err(BrowserError::LOST));
    fixture.daemon.state().fail.clear();
    runtime.clean().await.expect("cleaned");
    assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
    assert!(!runtime.dirty());
}

#[tokio::test(start_paused = true)]
async fn a_removal_under_way_elsewhere_or_still_listed_is_proved_like_any_other() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.daemon.state().conflict = true;
    fixture.daemon.state().linger = 3;
    runtime.release(&lease).await.expect("released");
    assert_eq!(fixture.daemon.state().linger, 0);
    fixture.daemon.state().conflict = false;
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.daemon.state().linger = 5;
    let started = Instant::now();
    runtime.release(&lease).await.expect("released");
    assert!(started.elapsed() >= 5 * super::REMOVE_POLL);
    // One that never goes hung: it is lost once the one deadline passed.
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.daemon.state().linger = 1_000_000;
    let started = Instant::now();
    assert_eq!(runtime.release(&lease).await, Err(BrowserError::LOST));
    assert!(started.elapsed() <= super::REMOVE_SECONDS);
    assert!(runtime.dirty());
}

#[tokio::test(start_paused = true)]
async fn a_stalled_removal_or_check_never_outlasts_its_deadline() {
    for stalled in ["remove", "inspect"] {
        let fixture = Fixture::new();
        let runtime = fixture.runtime(Some(image()));
        let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
        fixture.daemon.state().stall.insert(stalled);
        let started = Instant::now();
        assert_eq!(runtime.release(&lease).await, Err(BrowserError::LOST));
        assert_eq!(started.elapsed(), super::REMOVE_SECONDS);
        assert!(runtime.dirty());
    }
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    assert_eq!(runtime.remove("never-existed").await, Ok(()));
    fixture.daemon.state().fail.insert("inspect");
    assert_eq!(
        runtime.remove("never-existed").await,
        Err(BrowserError::LOST)
    );
}

#[tokio::test(start_paused = true)]
async fn a_team_teardown_removes_only_that_team_s_browser() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let _lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    runtime.remove_team("team_2").await.expect("removed");
    assert_eq!(fixture.daemon.browsers().len(), 1);
    runtime.remove_team("team_1").await.expect("removed");
    assert_eq!(fixture.daemon.browsers(), Vec::<String>::new());
}

#[tokio::test(start_paused = true)]
async fn the_downloads_tool_answers_are_strictly_framed() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    let listing = br#"{"files":[{"name":"edital.pdf","size":9}],"refused":[],"skipped":0}"#;
    let mut line = listing.to_vec();
    line.push(b'\n');
    fixture.execs.script(&["files", "list"], &line, 0);
    fixture
        .execs
        .script(&["files", "read", "edital.pdf"], b"9\n%PDF-1.4\n", 0);
    let expected = strict_json::parse(listing).expect("json");
    assert_eq!(
        runtime.files(&lease, None, None).await,
        Ok(expected.clone())
    );
    assert_eq!(
        runtime.read(&lease, "edital.pdf", 9, None).await,
        Ok(b"%PDF-1.4\n".to_vec())
    );
    for output in [
        &b"[]\n"[..],
        b"{\"files\": []}\n",
        b"not json\n",
        b"{\"files\":[]}",
        b"{\"files\":[],\"files\":[],\"refused\":[],\"skipped\":0}\n",
    ] {
        fixture.execs.script(&["files", "list"], output, 0);
        assert_eq!(
            runtime.files(&lease, None, None).await,
            Err(BrowserError::LOST)
        );
    }
    fixture.execs.script(&["files", "list"], &line, 0);
    // A listing asked by a deadline already past, or after Stop, never answers as one that was read.
    let stop = Stop::new();
    stop.stop();
    assert_eq!(
        runtime.files(&lease, Some(Instant::now()), None).await,
        Err(BrowserError::LOST)
    );
    assert_eq!(
        runtime.files(&lease, None, Some(&stop)).await,
        Err(BrowserError::LOST)
    );
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn a_download_is_read_exactly_as_its_stated_length() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    for output in [&b"8\n%PDF-1.4\n"[..], b"9\nshort", b"9\n%PDF-1.4\nextra"] {
        fixture
            .execs
            .script(&["files", "read", "edital.pdf"], output, 0);
        assert_eq!(
            runtime.read(&lease, "edital.pdf", 9, None).await,
            Err(BrowserError::LOST)
        );
    }
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn the_relay_and_the_bounded_proof_its_exec_ended() {
    let mut fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    let (exec_id, _channel) = runtime.open_relay(&lease).await.expect("opened");
    let (arguments, _far) = fixture.attached.recv().await.expect("attached");
    assert_eq!(arguments, ["relay"]);
    assert!(
        runtime
            .exited(&exec_id, Instant::now() + Duration::from_secs(1))
            .await
    );
    fixture
        .execs
        .state()
        .running
        .insert(vec!["relay".to_owned()]);
    let started = Instant::now();
    assert!(
        !runtime
            .exited(&exec_id, Instant::now() + Duration::from_millis(300))
            .await
    );
    assert!(started.elapsed() <= Duration::from_millis(400));
    assert!(!runtime.exited(&exec_id, Instant::now()).await);
    fixture.execs.state().running.clear();
    fixture.execs.state().fail.insert("inspect");
    assert!(
        !runtime
            .exited(&exec_id, Instant::now() + Duration::from_millis(200))
            .await
    );
    fixture.execs.state().fail.clear();
    // A stalled inspection proves nothing.
    fixture.execs.state().stall.insert("inspect");
    assert!(
        !runtime
            .exited(&exec_id, Instant::now() + Duration::from_millis(200))
            .await
    );
    fixture.execs.state().stall.clear();
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn a_stalled_driver_session_ends_at_stop() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    fixture.execs.state().stall.insert("exec");
    let stop = Stop::new();
    let stopper = stop.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(200)).await;
        stopper.stop();
    });
    let started = Instant::now();
    // The task's deadline is far: Stop alone ends the wait.
    assert_eq!(
        runtime
            .open_stream(&lease, Instant::now() + Duration::from_secs(3600), &stop)
            .await
            .map(|_| ()),
        Err(BrowserError::LOST)
    );
    assert!(started.elapsed() < Duration::from_secs(1));
    fixture.execs.state().stall.clear();
    fixture.execs.state().fail.insert("exec");
    assert_eq!(
        runtime
            .open_stream(
                &lease,
                Instant::now() + Duration::from_secs(10),
                &Stop::new()
            )
            .await
            .map(|_| ()),
        Err(BrowserError::LOST)
    );
    assert_eq!(
        runtime
            .program(
                &lease.container_id,
                &["ready"],
                Instant::now() + Duration::from_secs(10)
            )
            .await,
        None
    );
}

#[tokio::test(start_paused = true)]
async fn a_relaunch_must_answer_relaunched_with_status_zero_while_still_wanted() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let lease = runtime.acquire(agent("neo")).await.expect("acquired");
    let deadline = Instant::now() + Duration::from_secs(60);
    fixture.execs.script(&["relaunch"], b"relaunched\n", 0);
    assert_eq!(
        runtime.relaunch(&lease, deadline, &Stop::new()).await,
        Ok(())
    );
    for (answer, status) in [
        (&b"relaunched\n"[..], 1),
        (b"", 0),
        (b"relaunched\nmore\n", 0),
        (b"running\n", 0),
    ] {
        fixture.execs.script(&["relaunch"], answer, status);
        assert_eq!(
            runtime.relaunch(&lease, deadline, &Stop::new()).await,
            Err(BrowserError::LOST)
        );
    }
    fixture.execs.script(&["relaunch"], b"relaunched\n", 0);
    let stopped = Stop::new();
    stopped.stop();
    assert_eq!(
        runtime.relaunch(&lease, deadline, &stopped).await,
        Err(BrowserError::LOST)
    );
    fixture.execs.state().fail.insert("inspect");
    assert_eq!(
        runtime.relaunch(&lease, deadline, &Stop::new()).await,
        Err(BrowserError::LOST)
    );
    fixture.execs.state().fail.clear();
    runtime.release(&lease).await.expect("released");
}

#[tokio::test(start_paused = true)]
async fn startup_removes_a_leftover_browser_and_leaves_the_egress_alone() {
    let fixture = Fixture::new();
    let runtime = fixture.runtime(Some(image()));
    let leftover = runtime.acquire(agent("team_1")).await.expect("acquired");
    runtime.free_slot();
    let restarts = fixture.daemon.state().restarts;
    assert!(runtime.startup().await);
    assert!(!fixture.daemon.browsers().contains(&leftover.container_id));
    assert_eq!(fixture.daemon.state().restarts, restarts);
    fixture.daemon.state().fail.insert("list");
    assert!(!runtime.startup().await);
    fixture.daemon.state().fail.clear();
    // The runtime stays dirty after startup, so the first task still starts on a fresh egress generation.
    runtime
        .dirty
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let lease = runtime.acquire(agent("team_1")).await.expect("acquired");
    assert_eq!(fixture.daemon.state().restarts, restarts + 1);
    assert!(!runtime.startup().await);
    runtime.release(&lease).await.expect("released");
}

#[test]
fn a_team_image_names_its_browser_only_by_exact_digest() {
    let labelled = |value: &str| HashMap::from([(super::IMAGE_LABEL.to_owned(), value.to_owned())]);
    assert_eq!(browser_image(Some(&labelled(&image()))), Some(image()));
    assert_eq!(
        browser_image(Some(&labelled(&published()))),
        Some(published())
    );
    for value in [
        "localhost/shimpz-browser:latest".to_owned(),
        format!("docker.io/shimpz-browser@sha256:{}", "a".repeat(64)),
        format!("localhost/shimpz-browser@sha256:{}", "A".repeat(64)),
        format!("localhost/shimpz-browser@sha256:{}", "a".repeat(63)),
    ] {
        assert_eq!(browser_image(Some(&labelled(&value))), None, "{value}");
    }
    assert_eq!(browser_image::<std::hash::RandomState>(None), None);
    assert_eq!(browser_image(Some(&HashMap::new())), None);
}
