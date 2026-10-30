use std::time::Duration;

use strict_json::Value;

use super::{K, code, field, harness, int};
use crate::coordination::executions::ExecutionKind;
use crate::coordination::residue::{ResidueKind, TeardownFailed};
use crate::docker::DockerError;
use crate::docker::labels::{ASSISTANT, TEAM};
use crate::docker::recorded::Fault;

fn every_kind() -> Value {
    let mut names: Vec<&str> = ResidueKind::ALL.iter().map(|kind| kind.name()).collect();
    names.sort_unstable();
    Value::Array(
        names
            .into_iter()
            .map(|name| Value::String(name.to_owned()))
            .collect(),
    )
}

#[tokio::test]
async fn destroy_proves_all_17_residue_kinds() {
    let harness = harness();
    harness.populated("team_1", "Team One").await;
    harness.populated("team_2", "Team Two").await;
    let answer = harness
        .lifecycle
        .destroy("team_1", "Team One")
        .await
        .expect("destroyed");
    assert_eq!(
        field(&answer, "team_id"),
        &Value::String("team_1".to_owned())
    );
    assert_eq!(field(&answer, "destroyed"), &Value::Bool(true));
    assert_eq!(field(&answer, "assistants_removed"), &int(1));
    assert_eq!(field(&answer, "storage_removed"), &Value::Bool(true));
    assert_eq!(field(&answer, "residue_absent"), &every_kind());
    // Every owned object of the Team is gone; the other Team keeps all of its own.
    assert!(!harness.assistant_exists("team_1") && !harness.has_policy("team_1"));
    assert!(!harness.network_exists("team_1"));
    assert!(harness.docker.inner().container("helper-team_1").is_none());
    assert!(harness.assistant_exists("team_2") && harness.has_policy("team_2"));
    assert!(harness.network_exists("team_2"));
    assert!(harness.docker.inner().container("helper-team_2").is_some());
}

#[tokio::test]
async fn destroy_runs_its_owners_in_the_frozen_order() {
    let harness = harness();
    harness.populated("team_1", "Team One").await;
    harness
        .lifecycle
        .destroy("team_1", "Team One")
        .await
        .expect("destroyed");
    let events = &harness.events;
    // Paused turns end first; the conversation goes before any workload; the network goes after everything inside
    // it, the name only after the network, and the private Integrations and Stored Inputs after the name.
    let all = events.all();
    let at = |wanted: &str| all.iter().position(|event| event == wanted).expect(wanted);
    let removals: Vec<usize> = (0..all.len())
        .filter(|index| all[*index] == "remove id")
        .collect();
    // The helper's removal, then the Assistant container's.
    assert_eq!(removals.len(), 2, "{all:?}");
    assert!(at("brain_checkpoints+action_checkpoints team_1") < removals[0]);
    assert!(removals[0] < at("browsers team_1"));
    assert!(at("routines team_1") < removals[1]);
    assert!(removals[1] < at("publication_bindings team_1"));
    let order = [
        "chat_continuations team_1",
        "brain_checkpoints+action_checkpoints team_1",
        "browsers team_1",
        "sign_ins team_1",
        "routines team_1",
        "publication_bindings team_1",
        "inference_configuration team_1",
        "team_storage team_1",
        "remove-network",
        "integration_credentials+stored_inputs team_1",
    ];
    for pair in order.windows(2) {
        assert!(
            events.before(pair[0], pair[1]),
            "{pair:?} in {:?}",
            events.all()
        );
    }
    assert!(
        harness
            .names
            .load("team_1", "x")
            .expect("readable")
            .is_none()
    );
}

#[tokio::test]
async fn a_stale_name_deletes_nothing() {
    let harness = harness();
    harness.populated("team_1", "Marketing").await;
    harness
        .lifecycle
        .rename("team_1", "Growth")
        .await
        .expect("renamed");
    harness.events.clear();
    assert_eq!(
        code(harness.lifecycle.destroy("team_1", "Marketing").await),
        Some("team-name-mismatch")
    );
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.contains("team_1") || event.starts_with("remove"))
    );
    assert!(harness.assistant_exists("team_1"));
}

#[tokio::test]
async fn partial_teardown_is_error() {
    // A Brain failure stops the teardown before any workload or object is removed.
    let harness = harness();
    harness.populated("team_1", "Team One").await;
    *harness
        .owner(K::BrainCheckpoints)
        .failure
        .lock()
        .expect("usable") = Some(TeardownFailed::StateUnavailable);
    assert_eq!(
        code(harness.lifecycle.destroy("team_1", "Team One").await),
        Some("brain-runtime-failed")
    );
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.starts_with("remove"))
    );
    assert!(harness.assistant_exists("team_1") && harness.network_exists("team_1"));
    // A Browser that cannot be proven removed fails it after the helpers, and nothing later runs.
    *harness
        .owner(K::BrainCheckpoints)
        .failure
        .lock()
        .expect("usable") = None;
    *harness.owner(K::Browsers).failure.lock().expect("usable") = Some(TeardownFailed::Failed);
    harness.events.clear();
    assert_eq!(
        code(harness.lifecycle.destroy("team_1", "Team One").await),
        Some("docker-remove-failed")
    );
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.starts_with("sign_ins"))
    );
    assert!(harness.assistant_exists("team_1") && harness.network_exists("team_1"));
}

#[tokio::test]
async fn a_network_docker_cannot_remove_keeps_the_team_until_a_retry_completes_it() {
    let harness = harness();
    harness.populated("team_1", "Team One").await;
    harness
        .docker
        .fail("remove_network", Fault::Before(DockerError::Unavailable));
    assert_eq!(
        code(harness.lifecycle.destroy("team_1", "Team One").await),
        Some("docker-remove-failed")
    );
    assert!(harness.network_exists("team_1"));
    assert!(
        harness
            .lifecycle
            .destroy("team_1", "Team One")
            .await
            .is_ok()
    );
    // Already destroyed: the teardown confirms absence and still proves every kind.
    let again = harness
        .lifecycle
        .destroy("team_1", "Team One")
        .await
        .expect("idempotent");
    assert_eq!(field(&again, "destroyed"), &Value::Bool(false));
    assert_eq!(field(&again, "residue_absent").clone(), every_kind());
}

#[tokio::test]
async fn registry_drift_or_a_drifted_container_refuses_the_destruction_before_anything_changes() {
    let harness = harness();
    harness.populated("team_1", "Team One").await;
    harness.bound.unbind_all();
    assert_eq!(
        code(harness.lifecycle.destroy("team_1", "Team One").await),
        Some("ownership-conflict")
    );
    harness.bound.bind("team_1", super::spec());
    let name = harness.space.container_name("team_1", "notes");
    harness.docker.inner().tamper(&name, |inspection| {
        inspection.host_config.as_mut().expect("host").privileged = Some(true);
    });
    assert_eq!(
        code(harness.lifecycle.destroy("team_1", "Team One").await),
        Some("ownership-conflict")
    );
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.contains("team_1") || event.starts_with("remove"))
    );
    assert!(harness.assistant_exists("team_1"));
    let _ = (TEAM, ASSISTANT);
}

#[tokio::test(start_paused = true)]
async fn destroy_cancels_the_team_s_execution_and_waits_for_its_slot() {
    let harness = harness();
    harness.populated("team_1", "Team One").await;
    let execution = harness
        .coordination
        .executions
        .admit("team_1", ExecutionKind::Chat, "turn".to_owned())
        .expect("admitted");
    // The turn never leaves: its Stop is set, and after the drain bound the destruction refuses, removing nothing.
    assert_eq!(
        code(harness.lifecycle.destroy("team_1", "Team One").await),
        Some("chat-active")
    );
    assert!(execution.stop().is_stopped());
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.contains("team_1 ") || event.starts_with("remove"))
    );
    // A turn that leaves within the bound lets the destruction run.
    drop(execution);
    let execution = harness
        .coordination
        .executions
        .admit("team_1", ExecutionKind::Chat, "last".to_owned())
        .expect("admitted");
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(1)).await;
        drop(execution);
    });
    assert!(
        harness
            .lifecycle
            .destroy("team_1", "Team One")
            .await
            .is_ok()
    );
}
