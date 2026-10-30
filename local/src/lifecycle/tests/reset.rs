use std::sync::atomic::Ordering;

use strict_json::Value;

use super::{K, code, field, harness, int, spec};
use crate::coordination::residue::TeardownFailed;
use crate::docker::DockerError;
use crate::docker::labels::TEAM;
use crate::docker::recorded::Fault;

#[tokio::test]
async fn reset_proves_all_17_residue_kinds() {
    let harness = harness();
    harness.populated("team_1", "One").await;
    harness.populated("team_2", "Two").await;
    // A bound Assistant whose container a crash removed still loses its route.
    harness.bound.bind("team_3", super::spec());
    let answer = harness.lifecycle.reset().await.expect("reset");
    assert_eq!(field(&answer, "reset"), &Value::Bool(true));
    assert_eq!(field(&answer, "assistants_removed"), &int(2));
    assert_eq!(field(&answer, "teams_removed"), &int(2));
    assert_eq!(field(&answer, "storage_removed"), &int(2));
    let Value::Array(absent) = field(&answer, "residue_absent") else {
        panic!("a list");
    };
    assert_eq!(absent.len(), 17);
    for team_id in ["team_1", "team_2"] {
        assert!(!harness.assistant_exists(team_id) && !harness.has_policy(team_id));
        assert!(!harness.network_exists(team_id));
    }
    assert_eq!(
        harness.docker.inner().container_names(),
        [] as [std::string::String; 0]
    );
    assert_eq!(
        harness.docker.inner().network_names(),
        [] as [std::string::String; 0]
    );
}

#[tokio::test]
async fn reset_runs_its_owners_in_the_frozen_order() {
    let harness = harness();
    harness.populated("team_1", "One").await;
    harness.lifecycle.reset().await.expect("reset");
    let all = harness.events.all();
    let at = |wanted: &str| all.iter().position(|event| event == wanted).expect(wanted);
    let removals: Vec<usize> = (0..all.len())
        .filter(|index| all[*index] == "remove id")
        .collect();
    assert_eq!(removals.len(), 2, "{all:?}");
    // Private state first, then the conversations and Routines; Assistant containers, then their routes and
    // bindings; files, settings, networks, names; and the helpers, Browsers, and Sign-ins last.
    let order = [
        at("integration_credentials+stored_inputs space"),
        at("brain_checkpoints+action_checkpoints space"),
        at("routines space"),
        removals[0],
        at("publication_bindings space"),
        at("team_storage space"),
        at("inference_configuration space"),
        at("remove-network"),
        at("chat_continuations space"),
        removals[1],
        at("browsers space"),
        at("sign_ins space"),
    ];
    assert!(order.windows(2).all(|pair| pair[0] < pair[1]), "{all:?}");
}

#[tokio::test]
async fn reset_inventory_refuses_foreign_or_drifted_resources_before_anything_changes() {
    let harness = harness();
    harness.populated("team_1", "One").await;
    let name = harness.space.container_name("team_1", "notes");
    harness.docker.inner().tamper(&name, |inspection| {
        inspection.name = Some("/foreign-container".to_owned());
    });
    assert_eq!(
        code(harness.lifecycle.reset().await),
        Some("ownership-conflict")
    );
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.ends_with(" space") || event.starts_with("remove"))
    );
    let harness = super::harness();
    harness.populated("team_1", "One").await;
    harness
        .docker
        .inner()
        .tamper_network(&harness.space.network_name("team_1"), |network| {
            network
                .labels
                .as_mut()
                .expect("labels")
                .insert(TEAM.to_owned(), "Team 7".to_owned());
        });
    assert_eq!(
        code(harness.lifecycle.reset().await),
        Some("ownership-conflict")
    );
    assert!(harness.assistant_exists("team_1"));
    harness
        .docker
        .fail("list_containers", Fault::Before(DockerError::Unavailable));
    assert_eq!(
        code(harness.lifecycle.reset().await),
        Some("docker-reset-failed")
    );
}

#[tokio::test]
async fn a_participant_that_cannot_prove_its_state_gone_fails_the_reset() {
    for (kind, failure, expected) in [
        (K::Browsers, TeardownFailed::Failed, "docker-reset-failed"),
        (K::SignIns, TeardownFailed::Failed, "docker-reset-failed"),
        (
            K::TeamStorage,
            TeardownFailed::StateUnavailable,
            "storage-safety-failed",
        ),
        (
            K::InferenceConfiguration,
            TeardownFailed::StateUnavailable,
            "inference-store-failed",
        ),
    ] {
        let harness = harness();
        harness.populated("team_1", "One").await;
        *harness.owner(kind).failure.lock().expect("usable") = Some(failure);
        assert_eq!(
            code(harness.lifecycle.reset().await),
            Some(expected),
            "{kind:?}"
        );
    }
    let harness = harness();
    harness.populated("team_1", "One").await;
    harness
        .docker
        .fail("remove_network", Fault::Before(DockerError::Unavailable));
    assert_eq!(
        code(harness.lifecycle.reset().await),
        Some("docker-reset-failed")
    );
    // The network removal failed, so every Team keeps its name record.
    assert!(harness.network_exists("team_1"));
}

#[tokio::test]
async fn the_bootstrap_reset_needs_proof_that_no_supervisor_key_is_published() {
    let harness = harness();
    harness.populated("team_1", "One").await;
    harness.pin.established.store(true, Ordering::SeqCst);
    assert_eq!(
        code(harness.lifecycle.bootstrap_reset().await),
        Some("supervisor-established")
    );
    assert!(harness.assistant_exists("team_1"));
    assert!(!harness.pin.forgotten.load(Ordering::SeqCst));
    harness.pin.established.store(false, Ordering::SeqCst);
    let answer = harness.lifecycle.bootstrap_reset().await.expect("reset");
    assert_eq!(field(&answer, "reset"), &Value::Bool(true));
    assert!(harness.pin.forgotten.load(Ordering::SeqCst));
    assert!(!harness.assistant_exists("team_1"));
    let _ = spec();
}
