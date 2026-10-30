use std::collections::HashSet;

use super::{Authority, Method, ROUTES, Reply, resolve};
use crate::http::operation::Operation;

#[test]
fn every_operation_indexes_its_own_route_and_names_are_unique() {
    for (index, route) in ROUTES.iter().enumerate() {
        assert_eq!(route.operation as usize, index, "{}", route.name);
        assert_eq!(route.operation.name(), route.name);
    }
    let names: HashSet<_> = ROUTES.iter().map(|route| route.name).collect();
    assert_eq!(names.len(), 75);
}

#[test]
fn route_authority_table_exact() {
    let machine: Vec<_> = ROUTES
        .iter()
        .filter(|route| route.authority == Authority::Machine)
        .map(|route| route.name)
        .collect();
    assert_eq!(
        machine,
        [
            "assistant-integration-complete",
            "routine-claim",
            "routine-notices",
            "routine-notice-ack",
            "health",
            "activity",
            "space-bootstrap-reset",
        ]
    );
    let routine: Vec<_> = ROUTES
        .iter()
        .filter(|route| route.authority == Authority::Routine)
        .map(|route| route.name)
        .collect();
    assert_eq!(routine, ["routine-run"]);
    let host: Vec<_> = ROUTES
        .iter()
        .filter(|route| route.authority == Authority::SupervisorOrHostReset)
        .map(|route| route.name)
        .collect();
    assert_eq!(host, ["space-reset"]);
    assert_eq!(
        ROUTES
            .iter()
            .filter(|route| route.authority == Authority::Supervisor)
            .count(),
        66
    );
}

#[test]
fn streamed_and_upgraded_operations_are_exact() {
    let of = |reply| -> Vec<_> {
        ROUTES
            .iter()
            .filter(|route| route.reply == reply)
            .map(|route| route.name)
            .collect()
    };
    assert_eq!(
        of(Reply::Ndjson),
        [
            "chat",
            "chat-integration-submit",
            "chat-human-submit",
            "routine-run",
            "routine-human-submit",
            "routine-integration-submit",
        ]
    );
    assert_eq!(
        of(Reply::Upgrade),
        ["browser-view-stream", "browser-view-control"]
    );
}

#[test]
fn resolution_captures_segments_without_fallthrough() {
    let found =
        resolve(Method::Post, "/v1/teams/alpha/assistants/demo/actions/run").expect("routed");
    assert_eq!(found.operation, Operation::AssistantInvoke);
    assert_eq!(found.param("team_id"), Some("alpha"));
    assert_eq!(found.param("assistant_id"), Some("demo"));
    assert_eq!(found.param("action_id"), Some("run"));
    assert_eq!(
        resolve(Method::Post, "/v1/teams/alpha/assistants/local").map(|found| found.operation),
        Some(Operation::LocalAssistantInstall)
    );
    // A proposal id is captured before a Routine id: the proposal route is listed first.
    assert_eq!(
        resolve(Method::Post, "/v1/teams/alpha/routines/proposals/resume")
            .map(|found| found.operation),
        Some(Operation::RoutineProposalConfirm)
    );
    assert_eq!(resolve(Method::Get, "/v1/teams/alpha/chat/stop"), None);
    assert_eq!(
        resolve(Method::Get, "/v1/teams/alpha/files/extra/segments/here/too"),
        None
    );
    assert_eq!(
        resolve(Method::Get, "/healthz").map(|found| found.operation),
        Some(Operation::Health)
    );
}

#[test]
fn methods_parse_exactly() {
    assert_eq!(Method::parse("PATCH"), Some(Method::Patch));
    assert_eq!(Method::parse("get"), None);
    assert_eq!(Method::parse("HEAD"), None);
}
