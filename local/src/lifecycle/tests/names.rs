use strict_json::Value;

use super::{K, code, field, harness};
use crate::coordination::residue::TeardownFailed;
use crate::docker::DockerError;
use crate::docker::labels::{SPACE, TEAM, TEAM_NAME};
use crate::docker::recorded::Fault;

fn teams(list: &Value) -> Vec<(String, String)> {
    let Value::Array(teams) = field(list, "teams") else {
        panic!("a list");
    };
    teams
        .iter()
        .map(|team| {
            assert_eq!(field(team, "status"), &Value::String("running".to_owned()));
            match (field(team, "team_id"), field(team, "team_name")) {
                (Value::String(id), Value::String(name)) => (id.clone(), name.clone()),
                _ => panic!("a Team"),
            }
        })
        .collect()
}

#[tokio::test]
async fn create_is_idempotent_by_name_and_refuses_a_name_another_team_has() {
    let harness = harness();
    let created = harness
        .lifecycle
        .create("team_a", "Marketing")
        .await
        .expect("created");
    assert_eq!(field(&created, "created"), &Value::Bool(true));
    let again = harness
        .lifecycle
        .create("team_a", "Marketing")
        .await
        .expect("confirmed");
    assert_eq!(field(&again, "created"), &Value::Bool(false));
    harness
        .lifecycle
        .rename("team_a", "Growth")
        .await
        .expect("renamed");
    // The current name decides: the old label is another name now.
    assert_eq!(
        code(harness.lifecycle.create("team_a", "Marketing").await),
        Some("team-name-conflict")
    );
    assert_eq!(
        field(
            &harness
                .lifecycle
                .create("team_a", "Growth")
                .await
                .expect("same"),
            "created"
        ),
        &Value::Bool(false)
    );
    assert_eq!(
        code(harness.lifecycle.create("team_g", "growth").await),
        Some("team-name-taken")
    );
    assert!(!harness.network_exists("team_g"));
}

#[tokio::test]
async fn a_new_incarnation_starts_from_nothing_its_id_left() {
    let harness = harness();
    harness
        .names
        .save("team_c", "a".repeat(64).as_str(), "Old")
        .expect("a stale record");
    harness.events.clear();
    harness
        .lifecycle
        .create("team_c", "Research")
        .await
        .expect("created");
    // Its earlier Browser, files, settings, and Routines are cleared before the network exists, and the stale name
    // record is gone.
    for kind in [
        "browsers team_c",
        "team_storage team_c",
        "inference_configuration team_c",
        "routines team_c",
    ] {
        assert!(harness.events.before(kind, "create-network"), "{kind}");
    }
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.starts_with("sign_ins") || event.starts_with("brain"))
    );
    assert_eq!(
        harness.names.load("team_c", "a".repeat(64).as_str()),
        Ok(None)
    );
    // A Browser that cannot be removed refuses the creation: no network is made.
    *harness.owner(K::Browsers).failure.lock().expect("usable") = Some(TeardownFailed::Failed);
    assert_eq!(
        code(harness.lifecycle.create("team_d", "Sales").await),
        Some("docker-remove-failed")
    );
    assert!(!harness.network_exists("team_d"));
    *harness.owner(K::Browsers).failure.lock().expect("usable") = None;
    *harness
        .owner(K::TeamStorage)
        .failure
        .lock()
        .expect("usable") = Some(TeardownFailed::StateUnavailable);
    assert_eq!(
        code(harness.lifecycle.create("team_d", "Sales").await),
        Some("storage-safety-failed")
    );
}

#[tokio::test]
async fn a_refused_network_create_adopts_only_a_proven_concurrent_one() {
    let harness = harness();
    harness
        .docker
        .fail("create_network", Fault::Before(DockerError::Unavailable));
    assert_eq!(
        code(harness.lifecycle.create("team_a", "Alpha").await),
        Some("docker-create-failed")
    );
    // The daemon made it, but its answer was lost: the network is confirmed by its name.
    harness
        .docker
        .fail("create_network", Fault::After(DockerError::Unavailable));
    let created = harness
        .lifecycle
        .create("team_a", "Alpha")
        .await
        .expect("adopted");
    assert_eq!(field(&created, "created"), &Value::Bool(false));
}

type Change = fn(&mut bollard::models::NetworkInspect);

/// One drift per attribute a Team network is admitted by.
fn changes() -> [(&'static str, Change); 7] {
    [
        ("internal", |network| network.internal = Some(false)),
        ("attachable", |network| network.attachable = Some(true)),
        ("driver", |network| {
            network.driver = Some("overlay".to_owned());
        }),
        ("name", |network| network.name = Some("foreign".to_owned())),
        ("space label", |network| {
            network
                .labels
                .as_mut()
                .expect("labels")
                .insert(SPACE.to_owned(), "foreign".to_owned());
        }),
        ("empty name label", |network| {
            network
                .labels
                .as_mut()
                .expect("labels")
                .insert(TEAM_NAME.to_owned(), String::new());
        }),
        ("team label not an id", |network| {
            network
                .labels
                .as_mut()
                .expect("labels")
                .insert(TEAM.to_owned(), "Team 7".to_owned());
        }),
    ]
}

#[tokio::test]
async fn team_network_internal_non_attachable_relabel_check() {
    for (case, change) in changes() {
        let harness = harness();
        harness
            .lifecycle
            .create("team_1", "One")
            .await
            .expect("created");
        harness
            .docker
            .inner()
            .tamper_network(&harness.space.network_name("team_1"), change);
        // A network outside the Space's labels is not listed as a Team at all; one renamed away is no Team's.
        let listed = code(harness.lifecycle.list().await);
        let renamed = code(harness.lifecycle.rename("team_1", "Two").await);
        match case {
            "space label" => assert_eq!((listed, renamed), (None, Some("ownership-conflict"))),
            "name" => assert_eq!(
                (listed, renamed),
                (Some("ownership-conflict"), Some("team-not-found"))
            ),
            _ => assert_eq!(
                (listed, renamed),
                (Some("ownership-conflict"), Some("ownership-conflict")),
                "{case}"
            ),
        }
    }
}

#[tokio::test]
async fn a_network_other_than_requested_is_never_adopted() {
    let harness = harness();
    harness
        .lifecycle
        .create("team_1", "One")
        .await
        .expect("created");
    harness
        .docker
        .inner()
        .tamper_network(&harness.space.network_name("team_1"), |network| {
            network.internal = Some(false);
        });
    assert_eq!(
        code(harness.lifecycle.create("team_1", "One").await),
        Some("ownership-conflict")
    );
    assert_eq!(
        code(harness.lifecycle.rename("team_1", "Two").await),
        Some("ownership-conflict")
    );
    harness.docker.inner().set_unavailable(true);
    assert_eq!(
        code(harness.lifecycle.list().await),
        Some("docker-unavailable")
    );
    assert_eq!(
        code(harness.lifecycle.rename("team_1", "Two").await),
        Some("docker-unavailable")
    );
}

#[tokio::test]
async fn a_rename_changes_only_the_display_name_and_returning_to_the_label_drops_the_record() {
    let harness = harness();
    harness
        .lifecycle
        .create("team_a", "Marketing")
        .await
        .expect("created");
    harness
        .lifecycle
        .create("team_b", "Sales")
        .await
        .expect("created");
    let renamed = harness
        .lifecycle
        .rename("team_a", "Growth")
        .await
        .expect("renamed");
    assert_eq!(
        field(&renamed, "team_name"),
        &Value::String("Growth".to_owned())
    );
    let listed = teams(&harness.lifecycle.list().await.expect("listed"));
    assert!(listed.contains(&("team_a".to_owned(), "Growth".to_owned())));
    assert_eq!(
        code(harness.lifecycle.rename("team_b", "GROWTH").await),
        Some("team-name-taken")
    );
    assert_eq!(
        code(harness.lifecycle.rename("team_c", "Other").await),
        Some("team-not-found")
    );
    harness
        .lifecycle
        .rename("team_a", "Marketing")
        .await
        .expect("back to the label");
    let network = harness.docker.inner().network_names();
    assert!(network.contains(&harness.space.network_name("team_a")));
    let id = harness
        .lifecycle
        .networks
        .require("team_a")
        .await
        .expect("present")
        .id;
    assert_eq!(harness.names.load("team_a", &id), Ok(None));
}

#[tokio::test]
async fn teams_list_newest_first_at_nanosecond_precision_across_offsets() {
    let harness = harness();
    let created = [
        ("team_a", "2026-09-30T12:00:00.000000001Z"),
        ("team_b", "2026-09-30T14:00:00.000000003+02:00"),
        ("team_c", "2026-09-30T12:00:00.000000002Z"),
        ("team_d", "2026-09-29T12:01:00.000000004-23:59"),
        ("team_e", "2026-09-30T12:00:00.000000003Z"),
    ];
    for (team_id, instant) in created {
        harness
            .lifecycle
            .create(team_id, team_id)
            .await
            .expect("created");
        harness
            .docker
            .inner()
            .tamper_network(&harness.space.network_name(team_id), |network| {
                network.created = Some(instant.to_owned());
            });
    }
    let order: Vec<String> = teams(&harness.lifecycle.list().await.expect("listed"))
        .into_iter()
        .map(|(id, _)| id)
        .collect();
    // team_d is the newest; team_b and team_e name the same instant, ordered by id.
    assert_eq!(order, ["team_d", "team_b", "team_e", "team_c", "team_a"]);
    harness
        .docker
        .inner()
        .tamper_network(&harness.space.network_name("team_a"), |network| {
            network.created = None;
        });
    assert_eq!(
        code(harness.lifecycle.list().await),
        Some("team-metadata-invalid")
    );
    // A rename never depends on creation metadata.
    assert!(harness.lifecycle.rename("team_b", "Renamed").await.is_ok());
}

async fn call(
    harness: &super::Harness,
    method: crate::http::route::Method,
    path: &str,
    body: crate::http::service::Body,
) -> Result<Value, crate::http::service::Problem> {
    use crate::http::service::{Answer, Call, Principal, Service};

    let route = crate::http::route::resolve(method, path).expect("a route");
    let call = Call {
        route: &route,
        principal: Principal::Machine,
        body,
        model: None,
        decision_key: None,
        trace_id: "0".repeat(32),
        upgrade_key: None,
    };
    match harness.lifecycle.call(call).await? {
        Answer::Json { status: 200, body } => Ok(body),
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn the_teams_service_takes_exactly_one_team_name_for_a_valid_team() {
    use crate::http::route::Method;
    use crate::http::service::Body;

    let harness = harness();
    let named = |name: &str| {
        Body::Json(Value::Object(vec![(
            "team_name".to_owned(),
            Value::String(name.to_owned()),
        )]))
    };
    let created = call(
        &harness,
        Method::Post,
        "/v1/teams/team_1/create",
        named("One"),
    )
    .await;
    assert_eq!(
        created.map(|answer| field(&answer, "created").clone()),
        Ok(Value::Bool(true))
    );
    let renamed = call(&harness, Method::Patch, "/v1/teams/team_1", named("Uno")).await;
    assert!(renamed.is_ok());
    let listed = call(&harness, Method::Get, "/v1/teams", Body::None)
        .await
        .expect("listed");
    assert_eq!(teams(&listed), [("team_1".to_owned(), "Uno".to_owned())]);
}

fn named(name: &str) -> crate::http::service::Body {
    crate::http::service::Body::Json(Value::Object(vec![(
        "team_name".to_owned(),
        Value::String(name.to_owned()),
    )]))
}

#[tokio::test]
async fn the_teams_service_refuses_any_other_body_or_team_id() {
    use crate::http::route::Method;
    use crate::http::service::Body;

    let harness = harness();
    harness
        .lifecycle
        .create("team_1", "Uno")
        .await
        .expect("created");
    let extra = Body::Json(Value::Object(vec![
        ("team_name".to_owned(), Value::String("Uno".to_owned())),
        ("other".to_owned(), Value::Bool(true)),
    ]));
    for body in [Body::None, extra, Body::Json(Value::Array(Vec::new()))] {
        let refused = call(&harness, Method::Delete, "/v1/teams/team_1", body).await;
        assert_eq!(code(refused), Some("invalid-body"));
    }
    let refused = call(&harness, Method::Delete, "/v1/teams/Team-1", named("Uno")).await;
    assert_eq!(code(refused), Some("invalid-team-id"));
    let refused = call(
        &harness,
        Method::Post,
        "/v1/teams/team_2/create",
        named(" padded"),
    )
    .await;
    assert_eq!(code(refused), Some("invalid-team-name"));
    let destroyed = call(&harness, Method::Delete, "/v1/teams/team_1", named("Uno")).await;
    assert_eq!(
        destroyed.map(|answer| field(&answer, "destroyed").clone()),
        Ok(Value::Bool(true))
    );
    let reset = call(&harness, Method::Delete, "/v1/space", Body::None).await;
    assert_eq!(
        reset.map(|answer| field(&answer, "reset").clone()),
        Ok(Value::Bool(true))
    );
}
