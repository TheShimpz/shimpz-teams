//! The Team's inference settings (`Group::Inference`, `local/inference.py`): which catalog provider and chat model the
//! Team's turns run on. A model price for the Browser's usage is read from the same catalog.

use std::borrow::Cow;
use std::sync::Arc;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_inference::config::{
    ConfigError, InferenceConfig, InferenceConfigStore, normalize, price,
};
use team_protocol::identifiers::canonical_team_id;
use team_protocol::python::value::object_of;

use super::ports::TeamView;
use super::problem;
use crate::coordination::locks::SpaceLocks;
use crate::http::operation::Operation;
use crate::http::service::{Answer, Body, Call, Problem, Service};

/// The Team id an operation's path names, refused as `invalid-team-id` otherwise.
///
/// # Errors
/// `422 invalid-team-id`.
pub fn team_id<'a>(call: &Call<'a>) -> Result<&'a str, Problem> {
    call.route
        .param("team_id")
        .filter(|team| canonical_team_id(&Value::String((*team).to_owned())).is_some())
        .ok_or(Problem::new(422, "invalid Team id", "invalid-team-id"))
}

/// The Team's inference settings could not be read or written.
#[must_use]
pub const fn store_failed() -> Problem {
    Problem::new(
        503,
        "Team model provider metadata is unavailable",
        "inference-store-failed",
    )
}

/// The answer of one inference setting.
fn settings(team_id: &str, config: &InferenceConfig) -> Value {
    object_of(vec![
        ("team_id", Value::String(team_id.to_owned())),
        ("provider", Value::String(config.provider.clone())),
        ("model", Value::String(config.model.clone())),
    ])
}

/// A model's catalog price in cents per million input and output tokens; an unknown or unnamed model is priced as the
/// provider's dearest, and an unknown provider as nothing.
#[must_use]
pub fn model_price(provider: &str, model: Option<&str>) -> (u64, u64) {
    price(provider, model).unwrap_or((0, 0))
}

/// The inference settings service.
pub struct InferenceService {
    team: Arc<dyn TeamView>,
    store: Arc<InferenceConfigStore>,
    locks: Arc<SpaceLocks>,
}

impl std::fmt::Debug for InferenceService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("InferenceService")
    }
}

impl InferenceService {
    /// The service over the Team view, the settings store, and the Team stripes.
    #[must_use]
    pub fn new(
        team: Arc<dyn TeamView>,
        store: Arc<InferenceConfigStore>,
        locks: Arc<SpaceLocks>,
    ) -> Self {
        Self { team, store, locks }
    }

    async fn status(&self, team_id: &str) -> Result<Value, Problem> {
        let _team = self.locks.team(team_id).await;
        self.team.network(team_id).await?;
        let store = Arc::clone(&self.store);
        let team = team_id.to_owned();
        let loaded = tokio::task::spawn_blocking(move || store.load(&team))
            .await
            .map_err(|_| store_failed())?;
        match loaded {
            Ok(config) => Ok(settings(team_id, &config)),
            Err(ConfigError::Missing) => Err(problem::inference_not_configured()),
            Err(_) => Err(store_failed()),
        }
    }

    async fn configure(&self, team_id: &str, body: &Body) -> Result<Value, Problem> {
        let Body::Json(body @ Value::Object(members)) = body else {
            return Err(Problem::new(
                422,
                "inference requires only provider and model",
                "invalid-body",
            ));
        };
        let exact =
            members.len() == 2 && body.get("provider").is_some() && body.get("model").is_some();
        if !exact {
            return Err(Problem::new(
                422,
                "inference requires only provider and model",
                "invalid-body",
            ));
        }
        let config = normalize(
            body.get("provider").unwrap_or(&Value::Null),
            body.get("model").unwrap_or(&Value::Null),
        )
        .map_err(|error| {
            let message = match error {
                ConfigError::UnsupportedProvider => {
                    "provider must be one of ['anthropic', 'openai']"
                }
                _ => "model is not supported by the selected provider",
            };
            Problem {
                status: 400,
                message: Cow::Borrowed(message),
                code: Some("invalid-inference"),
                retry_after_ms: None,
            }
        })?;
        let _team = self.locks.team(team_id).await;
        self.team.network(team_id).await?;
        let store = Arc::clone(&self.store);
        let (team, saved) = (team_id.to_owned(), config.clone());
        tokio::task::spawn_blocking(move || store.save(&team, &saved))
            .await
            .map_err(|_| store_failed())?
            .map_err(|_| store_failed())?;
        Ok(settings(team_id, &config))
    }
}

impl Service for InferenceService {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move {
            let team_id = team_id(&call)?;
            let body = match call.operation() {
                Operation::InferenceStatus => self.status(team_id).await?,
                Operation::InferenceConfigure => self.configure(team_id, &call.body).await?,
                _ => return Err(problem::internal_error()),
            };
            Ok(Answer::Json { status: 200, body })
        })
    }
}

#[cfg(test)]
mod tests;
