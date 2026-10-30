//! The closed answers of a capability plan and an intent route.

use strict_json::Value;
use team_protocol::python::text;
use team_protocol::python::value::string;
use team_protocol::turn::{
    MAX_CAPABILITY_SELECTED, MAX_INTENT_ROUTE_QUERY_CHARS, MAX_INTENT_ROUTE_REPLY_CHARS,
    MAX_INTENT_ROUTE_SELECTED,
};

use super::{
    Capability, CapabilityCandidate, CapabilityPlan, DirectoryCandidate, IntentRoute,
    LifecycleIntent, PlanStatus, RouteIntent, strictly_sorted, strings,
};

/// An answer object's members.
type Members = Vec<(String, Value)>;

fn exactly(members: &[(String, Value)], fields: &[&str]) -> bool {
    members.len() == fields.len()
        && members
            .iter()
            .all(|(name, _)| fields.contains(&name.as_str()))
}

fn member<'v>(members: &'v [(String, Value)], name: &str) -> &'v Value {
    members
        .iter()
        .find(|(member, _)| member == name)
        .map_or(&Value::Null, |(_, value)| value)
}

/// A capability plan's answer selecting only among `candidates`, or `None`.
#[must_use]
pub fn parse_capability_plan(
    value: &Value,
    candidates: &[CapabilityCandidate],
) -> Option<CapabilityPlan> {
    let Value::Object(members) = value else {
        return None;
    };
    if !exactly(members, &["status", "assistant_ids"]) {
        return None;
    }
    let status = match string(member(members, "status"))? {
        "sufficient" => PlanStatus::Sufficient,
        "install-required" => PlanStatus::InstallRequired,
        _ => return None,
    };
    let assistant_ids = strings(member(members, "assistant_ids"))?;
    let known = assistant_ids
        .iter()
        .all(|id| candidates.iter().any(|candidate| &candidate.id == id));
    let admitted = known
        && strictly_sorted(&assistant_ids)
        && assistant_ids.len() <= MAX_CAPABILITY_SELECTED
        && (status == PlanStatus::Sufficient) == assistant_ids.is_empty();
    admitted.then_some(CapabilityPlan {
        status,
        assistant_ids,
    })
}

/// The capability answer exactly when it was asked, and `not-needed` only for an ordinary task; the members left.
fn capability(members: &[(String, Value)], asked: bool) -> Option<(Option<Capability>, Members)> {
    let rest: Members = members
        .iter()
        .filter(|(name, _)| !asked || name != "capability")
        .cloned()
        .collect();
    if !asked {
        return Some((None, rest));
    }
    let answer = match string(member(members, "capability"))? {
        "not-needed" if string(member(members, "intent")) == Some("ordinary-task") => {
            Capability::NotNeeded
        }
        "possible" => Capability::Possible,
        _ => return None,
    };
    Some((Some(answer), rest))
}

fn query_admitted(query: &str) -> bool {
    text::strip(query) == query
        && text::len(query) <= MAX_INTENT_ROUTE_QUERY_CHARS
        && query
            .chars()
            .all(|character| !text::category(character).starts_with('C'))
}

fn reply_admitted(reply: &str) -> bool {
    text::strip(reply) == reply
        && text::len(reply) <= MAX_INTENT_ROUTE_REPLY_CHARS
        && reply.chars().all(|character| {
            let category = text::category(character);
            !category.starts_with('C') && category != "Zl" && category != "Zp"
        })
}

/// The route's members once admitted: intent, query, ids, reply, and whether a task follows.
fn route_members(
    members: &[(String, Value)],
    expected: Option<LifecycleIntent>,
) -> Option<IntentRoute> {
    if !exactly(
        members,
        &["intent", "query", "assistant_ids", "reply", "task_follows"],
    ) {
        return None;
    }
    let Value::Bool(task_follows) = member(members, "task_follows") else {
        return None;
    };
    let intent = RouteIntent::parse(string(member(members, "intent"))?)?;
    let query = string(member(members, "query"))?;
    let continues_install =
        expected.is_none() && intent == RouteIntent::Install && !query.is_empty();
    let reply = string(member(members, "reply"))?;
    let assistant_ids = strings(member(members, "assistant_ids"))?;
    let admitted = (!task_follows || continues_install)
        && query_admitted(query)
        && reply_admitted(reply)
        && strictly_sorted(&assistant_ids);
    admitted.then(|| IntentRoute {
        intent,
        query: query.to_owned(),
        assistant_ids,
        reply: reply.to_owned(),
        task_follows: *task_follows,
        capability: None,
    })
}

fn classification(route: IntentRoute) -> Option<IntentRoute> {
    let quiet = matches!(
        route.intent,
        RouteIntent::OrdinaryTask | RouteIntent::Unresolved
    );
    if !route.assistant_ids.is_empty() || (quiet && !route.query.is_empty()) {
        return None;
    }
    let lifecycle = matches!(route.intent, RouteIntent::Install | RouteIntent::Uninstall);
    let requires_reply =
        route.intent == RouteIntent::Unresolved || (lifecycle && route.query.is_empty());
    (route.reply.is_empty() != requires_reply).then_some(route)
}

fn selection(
    route: IntentRoute,
    expected: LifecycleIntent,
    candidates: &[DirectoryCandidate],
) -> Option<IntentRoute> {
    let ids = &route.assistant_ids;
    if route.intent == RouteIntent::Unresolved
        && route.query.is_empty()
        && ids.is_empty()
        && !route.reply.is_empty()
    {
        return Some(IntentRoute {
            task_follows: false,
            ..route
        });
    }
    let intent = match expected {
        LifecycleIntent::Install => RouteIntent::Install,
        LifecycleIntent::Uninstall => RouteIntent::Uninstall,
    };
    let known = ids
        .iter()
        .all(|id| candidates.iter().any(|candidate| &candidate.id == id));
    let admitted = route.intent == intent
        && route.query.is_empty()
        && !ids.is_empty()
        && ids.len() <= MAX_INTENT_ROUTE_SELECTED
        && known
        && (expected == LifecycleIntent::Install || ids.len() == 1)
        && route.reply.is_empty();
    admitted.then_some(route)
}

/// An intent route's answer for the `expected` lifecycle intent among `candidates`, its capability answer exactly when
/// `asked`, or `None`.
#[must_use]
pub fn parse_intent_route(
    value: &Value,
    expected: Option<LifecycleIntent>,
    candidates: &[DirectoryCandidate],
    asked: bool,
) -> Option<IntentRoute> {
    let Value::Object(members) = value else {
        return None;
    };
    let (answer, rest) = capability(members, asked)?;
    let route = route_members(&rest, expected)?;
    match expected {
        None => classification(route).map(|route| IntentRoute {
            capability: answer,
            ..route
        }),
        Some(expected) => selection(route, expected, candidates),
    }
}
