//! A task's Cua calls on its session (ADR-0126): the session's agent endpoint, behind the channel the Cua session speaks.
//!
//! The Cua session writes one JSON-RPC line and reads its answer. Here that line's tool call is one request to easi.run,
//! whose result is the answer. A call that gets no answer may still be acting on the page, since easi.run keeps an
//! admitted call running after its caller left; it marks the session uncertain, so no call follows it on this Browser.

use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use strict_json::{Number, Value};
use team_browser::cua::{Channel, ChannelLost};
use team_core::future::BoxFuture;
use team_protocol::python::json::{Options, dumps};
use tokio::time::Instant;

use super::BrowserRuntime;

const COMPACT: Options = Options {
    ensure_ascii: true,
    compact: true,
    sort_keys: false,
};

/// One Cua session's calls on one easi.run session.
pub struct AgentCalls {
    runtime: Arc<BrowserRuntime>,
    session_id: String,
    uncertain: Arc<AtomicBool>,
    answers: VecDeque<Vec<u8>>,
}

impl AgentCalls {
    /// The calls on `session_id`, setting `uncertain` when one has no answer.
    #[must_use]
    pub fn new(
        runtime: Arc<BrowserRuntime>,
        session_id: String,
        uncertain: Arc<AtomicBool>,
    ) -> Self {
        Self {
            runtime,
            session_id,
            uncertain,
            answers: VecDeque::new(),
        }
    }

    fn answer(&mut self, id: Value, member: &str, value: Value) {
        let line = Value::Object(vec![
            ("jsonrpc".to_owned(), Value::String("2.0".to_owned())),
            ("id".to_owned(), id),
            (member.to_owned(), value),
        ]);
        self.answers.push_back(dumps(&line, COMPACT).into_bytes());
    }

    async fn called(
        &mut self,
        id: Value,
        tool: &str,
        arguments: Value,
        deadline: Instant,
    ) -> Result<(), ChannelLost> {
        // Marked before the call and cleared after its answer: a dropped call leaves it set.
        self.uncertain.store(true, Ordering::SeqCst);
        let answer = self
            .runtime
            .tool(&self.session_id, tool, arguments, deadline)
            .await
            .map_err(|_| ChannelLost)?;
        self.uncertain.store(false, Ordering::SeqCst);
        if answer.status == 413 {
            return Err(ChannelLost);
        }
        let Ok(result) = answer.json() else {
            return Err(ChannelLost);
        };
        if answer.ok() {
            self.answer(id, "result", result);
        } else {
            let error = Value::Object(vec![
                ("code".to_owned(), Value::Number(Number::Int(-32000))),
                ("message".to_owned(), Value::String("refused".to_owned())),
            ]);
            self.answer(id, "error", error);
        }
        Ok(())
    }
}

impl Channel for AgentCalls {
    fn send<'a>(
        &'a mut self,
        data: &'a [u8],
        deadline: Instant,
    ) -> BoxFuture<'a, Result<(), ChannelLost>> {
        Box::pin(async move {
            let message = strict_json::parse(data.trim_ascii()).map_err(|_| ChannelLost)?;
            let id = message.get("id").cloned();
            let method = match message.get("method") {
                Some(Value::String(method)) => method.clone(),
                _ => return Err(ChannelLost),
            };
            match (method.as_str(), id) {
                ("tools/call", Some(id)) => {
                    let params = message.get("params").ok_or(ChannelLost)?;
                    let Some(Value::String(tool)) = params.get("name") else {
                        return Err(ChannelLost);
                    };
                    let tool = tool.clone();
                    let arguments = params
                        .get("arguments")
                        .cloned()
                        .unwrap_or_else(|| Value::Object(Vec::new()));
                    self.called(id, &tool, arguments, deadline).await
                }
                // easi.run has no session of its own to open: the endpoint answers calls at once.
                (_, Some(id)) => {
                    self.answer(id, "result", Value::Object(Vec::new()));
                    Ok(())
                }
                (_, None) => Ok(()),
            }
        })
    }

    fn receive_line(
        &mut self,
        _deadline: Instant,
        limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        Box::pin(async move {
            match self.answers.pop_front() {
                Some(line) if line.len() <= limit => Ok(line),
                _ => Err(ChannelLost),
            }
        })
    }
}

#[cfg(test)]
mod tests;
