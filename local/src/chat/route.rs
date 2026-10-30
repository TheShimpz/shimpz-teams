//! Bounded presentation intelligence that never grants Assistant authority (`local/chat/capabilities.py`): which
//! public Assistants a task needs, and which lifecycle intent a message carries.

use strict_json::Value;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_inference::brain::ModelCredential;
use team_inference::route::{
    Capability, CapabilityCandidate, CapabilityIntegration, DirectoryCandidate, Installable,
    IntentRouteRequest, LifecycleContext, LifecycleIntent, LifecycleReference, PlanStatus,
    RouteIntent, capability_plan, capability_plan_admitted, intent_route, intent_route_admitted,
};
use team_inference::runtime::ConversationEntry;
use team_protocol::python::value::{object_of, string};

use super::inference::team_id;
use super::problem;
use super::service::ChatService;
use crate::http::service::{Body, Call, Problem};

fn invalid(message: &'static str) -> Problem {
    Problem::new(422, message, "invalid-body")
}

fn exactly<'v>(value: &'v Value, fields: &[&str]) -> Option<&'v Value> {
    match value {
        Value::Object(members)
            if members.len() == fields.len()
                && members
                    .iter()
                    .all(|(name, _)| fields.contains(&name.as_str())) =>
        {
            Some(value)
        }
        _ => None,
    }
}

fn text(value: &Value, name: &str) -> Option<String> {
    value.get(name).and_then(string).map(str::to_owned)
}

fn texts(value: Option<&Value>) -> Option<Vec<String>> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| string(item).map(str::to_owned))
            .collect(),
        _ => None,
    }
}

fn candidate(value: &Value) -> Option<CapabilityCandidate> {
    let value = exactly(value, &["id", "name", "summary", "actions", "integrations"])?;
    let Some(Value::Array(integrations)) = value.get("integrations") else {
        return None;
    };
    let integrations = integrations
        .iter()
        .map(|item| {
            let item = exactly(item, &["id", "provider"])?;
            Some(CapabilityIntegration {
                id: text(item, "id")?,
                provider: text(item, "provider")?,
            })
        })
        .collect::<Option<Vec<_>>>()?;
    Some(CapabilityCandidate {
        id: text(value, "id")?,
        name: text(value, "name")?,
        summary: text(value, "summary")?,
        actions: texts(value.get("actions"))?,
        integrations,
    })
}

/// A capability plan's objective and candidates, admitted before anything reaches the Brain.
fn plan_input(body: &Body) -> Result<(String, Vec<CapabilityCandidate>), Problem> {
    let Body::Json(body) = body else {
        return Err(invalid(
            "capability plan requires only objective and candidates",
        ));
    };
    let body = exactly(body, &["objective", "candidates"])
        .ok_or_else(|| invalid("capability plan requires only objective and candidates"))?;
    let Some(Value::Array(candidates)) = body.get("candidates") else {
        return Err(invalid("capability plan candidates are invalid"));
    };
    let candidates = candidates.iter().map(candidate).collect::<Option<Vec<_>>>();
    let objective = text(body, "objective");
    match (objective, candidates) {
        (Some(objective), Some(candidates))
            if capability_plan_admitted(&objective, &candidates) =>
        {
            Ok((objective, candidates))
        }
        _ => Err(invalid("capability plan input is invalid")),
    }
}

fn conversation(value: Option<&Value>) -> Option<Vec<ConversationEntry>> {
    let Some(Value::Array(entries)) = value else {
        return None;
    };
    entries
        .iter()
        .map(|entry| {
            let entry = exactly(entry, &["role", "text", "truncated"])?;
            let Some(Value::Bool(truncated)) = entry.get("truncated") else {
                return None;
            };
            Some(ConversationEntry {
                role: text(entry, "role")?,
                text: text(entry, "text")?,
                truncated: *truncated,
            })
        })
        .collect()
}

fn lifecycle(body: &Value) -> Option<LifecycleContext> {
    let reference = match body.get("lifecycle_reference") {
        Some(Value::Null) => None,
        Some(reference) => {
            let reference = exactly(reference, &["id", "name"])?;
            Some(LifecycleReference {
                id: text(reference, "id")?,
                name: text(reference, "name")?,
            })
        }
        None => return None,
    };
    let locale = match body.get("locale") {
        Some(Value::Null) => None,
        locale => Some(locale.and_then(string)?.to_owned()),
    };
    Some(LifecycleContext {
        reference,
        conversation: conversation(body.get("conversation"))?,
        locale,
    })
}

fn installable(value: Option<&Value>) -> Option<Vec<Installable>> {
    let Some(value) = value else {
        return Some(Vec::new());
    };
    let Value::Array(items) = value else {
        return None;
    };
    items
        .iter()
        .map(|item| {
            let item = exactly(item, &["name", "summary"])?;
            Some(Installable {
                name: text(item, "name")?,
                summary: text(item, "summary")?,
            })
        })
        .collect()
}

const ROUTE_FIELDS: [&str; 6] = [
    "objective",
    "expected_intent",
    "candidates",
    "lifecycle_reference",
    "conversation",
    "locale",
];

fn route_request(body: &Value) -> Option<IntentRouteRequest> {
    let expected = match body.get("expected_intent") {
        Some(Value::Null) => None,
        Some(Value::String(intent)) if intent == "assistant-install" => {
            Some(LifecycleIntent::Install)
        }
        Some(Value::String(intent)) if intent == "assistant-uninstall" => {
            Some(LifecycleIntent::Uninstall)
        }
        _ => return None,
    };
    let Some(Value::Array(candidates)) = body.get("candidates") else {
        return None;
    };
    let candidates = candidates
        .iter()
        .map(|item| {
            let item = exactly(item, &["id", "name", "summary"])?;
            Some(DirectoryCandidate {
                id: text(item, "id")?,
                name: text(item, "name")?,
                summary: text(item, "summary")?,
            })
        })
        .collect::<Option<Vec<_>>>()?;
    Some(IntentRouteRequest {
        objective: text(body, "objective")?,
        expected,
        candidates,
        context: lifecycle(body)?,
        installable: installable(body.get("installable"))?,
    })
}

fn route_input(body: &Body) -> Result<Value, Problem> {
    let shape =
        "intent route requires objective, expected_intent, candidates, and lifecycle context";
    let Body::Json(body @ Value::Object(members)) = body else {
        return Err(invalid(shape));
    };
    let known = members
        .iter()
        .all(|(name, _)| name == "installable" || ROUTE_FIELDS.contains(&name.as_str()));
    let complete = ROUTE_FIELDS.iter().all(|name| body.get(name).is_some());
    if !known || !complete {
        return Err(invalid(shape));
    }
    if !matches!(body.get("candidates"), Some(Value::Array(_))) {
        return Err(invalid("intent route candidates are invalid"));
    }
    Ok(body.clone())
}

impl ChatService {
    /// The Team's network and model the decision runs on, read again after it, so a change in between is refused.
    async fn snapshot(
        &self,
        team_id: &str,
        provider: &str,
    ) -> Result<(String, String, String), Problem> {
        let _team = self.parts.coordination.locks.team(team_id).await;
        let scope = self.parts.team.scope(team_id, false).await?;
        let config = self.turn_inference(team_id, provider).await?;
        Ok((scope.network_id, config.provider, config.model))
    }

    /// Which public candidates a task needs (ADR-0079).
    pub(super) async fn capability_plan(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let team_id = team_id(call)?;
        let request = self.request(call)?;
        let (objective, candidates) = plan_input(&call.body)?;
        let before = self.snapshot(team_id, &request.credential.0).await?;
        let credential = ModelCredential {
            provider: before.1.clone(),
            model: before.2.clone(),
            api_key: request.credential.1.clone(),
        };
        let unavailable = Problem::new(
            503,
            "Assistant capability planning is unavailable",
            "capability-plan-unavailable",
        );
        let plan = capability_plan(
            self.parts.brain.as_ref(),
            request.usage.as_ref(),
            &credential,
            &objective,
            &candidates,
            &Stop::new(),
        )
        .await
        .map_err(|_| unavailable)?;
        if self.snapshot(team_id, &request.credential.0).await? != before {
            return Err(problem::team_context_changed());
        }
        let status = match plan.status {
            PlanStatus::Sufficient => "sufficient",
            PlanStatus::InstallRequired => "install-required",
        };
        Ok(object_of(vec![
            ("team_id", Value::String(team_id.to_owned())),
            ("status", Value::String(status.to_owned())),
            (
                "assistant_ids",
                Value::Array(plan.assistant_ids.into_iter().map(Value::String).collect()),
            ),
        ]))
    }

    /// Routes one objective's lifecycle intent without exposing Team state or granting lifecycle authority.
    pub(super) async fn intent_route(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let team_id = team_id(call)?;
        let request = self.request(call)?;
        let body = route_input(&call.body)?;
        let route = route_request(&body).ok_or_else(|| invalid("intent route input is invalid"))?;
        let decision: Option<&Secret> = call.decision_key.as_ref();
        decision_admitted(&route, decision)?;
        let before = self.snapshot(team_id, &request.credential.0).await?;
        let credential = ModelCredential {
            provider: before.1.clone(),
            model: before.2.clone(),
            api_key: request.credential.1.clone(),
        };
        if !intent_route_admitted(&credential, decision, &route) {
            return Err(invalid("intent route input is invalid"));
        }
        let unavailable = Problem::new(
            503,
            "Assistant lifecycle routing is unavailable",
            "intent-route-unavailable",
        );
        let (client, usage) = (self.parts.brain.as_ref(), request.usage.as_ref());
        let answer = intent_route(client, usage, &credential, decision, &route, &Stop::new())
            .await
            .map_err(|_| unavailable)?;
        if self.snapshot(team_id, &request.credential.0).await? != before {
            return Err(problem::team_context_changed());
        }
        Ok(route_answer(
            team_id,
            &answer,
            !route.installable.is_empty(),
        ))
    }
}

/// A decision credential applies only to a classification, and only it may describe installable Assistants.
fn decision_admitted(route: &IntentRouteRequest, decision: Option<&Secret>) -> Result<(), Problem> {
    if decision.is_some() && route.expected.is_some() {
        return Err(Problem::new(
            422,
            "a decision credential applies only to intent classification",
            "invalid-decision-credential",
        ));
    }
    if !route.installable.is_empty() && decision.is_none() {
        return Err(invalid(
            "installable Assistants are described only to the decision provider",
        ));
    }
    Ok(())
}

fn route_answer(
    team_id: &str,
    route: &team_inference::route::IntentRoute,
    described: bool,
) -> Value {
    let intent = match route.intent {
        RouteIntent::OrdinaryTask => "ordinary-task",
        RouteIntent::Install => "assistant-install",
        RouteIntent::Uninstall => "assistant-uninstall",
        RouteIntent::Unresolved => "unresolved",
    };
    let mut members = vec![
        ("team_id", Value::String(team_id.to_owned())),
        ("intent", Value::String(intent.to_owned())),
        ("query", Value::String(route.query.clone())),
        (
            "assistant_ids",
            Value::Array(
                route
                    .assistant_ids
                    .iter()
                    .cloned()
                    .map(Value::String)
                    .collect(),
            ),
        ),
        ("reply", Value::String(route.reply.clone())),
        ("task_follows", Value::Bool(route.task_follows)),
    ];
    if described {
        let capability = match route.capability {
            Some(Capability::NotNeeded) => Value::String("not-needed".to_owned()),
            Some(Capability::Possible) => Value::String("possible".to_owned()),
            None => Value::Null,
        };
        members.push(("capability", capability));
    }
    object_of(members)
}
