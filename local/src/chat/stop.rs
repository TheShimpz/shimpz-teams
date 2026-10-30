//! A person stops the Team's chat turn (`local/chat/resume.py`): its slot is cancelled before any challenge is
//! withdrawn, so a pause either committed already and its challenge goes here, or its commit fails and rolls it back.

use strict_json::Value;
use team_protocol::python::value::object_of;

use super::inference::team_id;
use super::service::ChatService;
use crate::http::service::{Body, Call, Problem};

impl ChatService {
    /// Cancels the Team's chat turn (a Routine run holding the slot has its own exact Stop), ends what it paused, and
    /// reports whether an Action was executing under it.
    pub(super) async fn stop(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let team_id = team_id(call)?;
        if !matches!(&call.body, Body::Json(Value::Object(members)) if members.is_empty()) {
            return Err(Problem::new(
                422,
                "chat stop requires an empty object",
                "invalid-body",
            ));
        }
        let (token, integration, human) = {
            let _team = self.parts.coordination.locks.team(team_id).await;
            self.parts.team.network(team_id).await?;
            let token = self.parts.coordination.executions.cancel(team_id, true);
            let integration = self
                .parts
                .integration_challenges
                .withdraw_team(team_id)
                .ok()
                .flatten();
            let human = self.human.withdraw_team(team_id).ok().flatten();
            let _ = self.parts.pkce.cancel_team(team_id);
            (token, integration, human)
        };
        let confirmed = token
            .as_ref()
            .is_some_and(|token| self.executing().get(team_id) == Some(token));
        // Only the continuations Stop withdrew are deleted: a turn paused since keeps its own.
        let mut deleted = false;
        if let Some(challenge) = &integration {
            deleted |= self.delete_continuation(team_id, Some(&challenge.id))?;
        }
        if let Some(challenge) = &human {
            deleted |= self.delete_continuation(team_id, Some(&challenge.id))?;
            // Only the paused turn's own batch: a turn started since keeps its batch (ADR-0038).
            self.purge(team_id, &challenge.payload).await?;
        }
        let accepted = token.is_some() || integration.is_some() || human.is_some() || deleted;
        Ok(object_of(vec![
            ("team_id", Value::String(team_id.to_owned())),
            ("requested", Value::Bool(accepted)),
            ("accepted", Value::Bool(accepted)),
            ("confirmed", Value::Bool(confirmed)),
            ("forced_restart", Value::Bool(false)),
        ]))
    }
}
