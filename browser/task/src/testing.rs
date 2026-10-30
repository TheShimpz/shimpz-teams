//! Test fakes: a scripted Driver channel that answers each request from queued replies and records what Team sent.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use strict_json::Value;
use team_core::future::BoxFuture;
use tokio::time::{Instant, sleep};

use crate::cua::{Channel, ChannelLost};

/// A JSON value from its text, members in the written order.
pub(crate) fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("a test value is JSON")
}

/// One queued reply: the members of a JSON-RPC answer, merged after `jsonrpc` and the request's `id`.
#[derive(Clone, Debug)]
pub(crate) enum Reply {
    Answer(Value),
}

/// `{"result": {"structuredContent": structured, **extra}}`.
pub(crate) fn tool(structured: &str) -> Reply {
    tool_with(structured, "{}")
}

/// `{"result": {"structuredContent": structured, **extra}}` with `extra` members.
pub(crate) fn tool_with(structured: &str, extra: &str) -> Reply {
    let mut result = vec![("structuredContent".to_owned(), json(structured))];
    if let Value::Object(members) = json(extra) {
        result.extend(members);
    }
    Reply::Answer(Value::Object(vec![(
        "result".to_owned(),
        Value::Object(result),
    )]))
}

/// An answer's members from JSON text.
pub(crate) fn answer(members: &str) -> Reply {
    Reply::Answer(json(members))
}

type Delay = Box<dyn FnMut(&Value, usize, Instant) -> Duration + Send>;

#[derive(Default)]
struct Inner {
    replies: VecDeque<Reply>,
    raw: VecDeque<Vec<u8>>,
    sent: Vec<Value>,
    lines: Vec<String>,
    deadlines: Vec<Instant>,
    send_delay: Option<Delay>,
    receive_delay: Duration,
    broken: bool,
    refuse_containing: Option<&'static str>,
}

/// The scripted Driver; clones share it, so a test inspects it after the session took its channel.
#[derive(Clone, Default)]
pub(crate) struct Script(Arc<Mutex<Inner>>);

impl Script {
    pub(crate) fn new(replies: impl IntoIterator<Item = Reply>) -> Self {
        let script = Self::default();
        script.extend(replies);
        script
    }

    fn inner(&self) -> MutexGuard<'_, Inner> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    pub(crate) fn extend(&self, replies: impl IntoIterator<Item = Reply>) {
        self.inner().replies.extend(replies);
    }

    pub(crate) fn raw(&self, line: &[u8]) {
        self.inner().raw.push_back(line.to_vec());
    }

    /// Each send first waits what `delay` says for the message, the count of messages sent before it, and its deadline.
    pub(crate) fn delay_sends(
        &self,
        delay: impl FnMut(&Value, usize, Instant) -> Duration + Send + 'static,
    ) {
        self.inner().send_delay = Some(Box::new(delay));
    }

    pub(crate) fn delay_receives(&self, delay: Duration) {
        self.inner().receive_delay = delay;
    }

    /// Every receive fails as a closed channel.
    pub(crate) fn break_receives(&self) {
        self.inner().broken = true;
    }

    /// A send whose bytes contain `text` fails as a closed channel.
    pub(crate) fn refuse_sends_containing(&self, text: &'static str) {
        self.inner().refuse_containing = Some(text);
    }

    pub(crate) fn sent(&self) -> Vec<Value> {
        self.inner().sent.clone()
    }

    /// Every line sent, exactly as encoded.
    pub(crate) fn lines(&self) -> Vec<String> {
        self.inner().lines.clone()
    }

    pub(crate) fn clear_sent(&self) {
        self.inner().sent.clear();
    }

    pub(crate) fn deadlines(&self) -> Vec<Instant> {
        self.inner().deadlines.clone()
    }

    pub(crate) fn left(&self) -> usize {
        self.inner().replies.len()
    }

    /// Every tool call sent: its name and arguments.
    pub(crate) fn calls(&self) -> Vec<(String, Value)> {
        self.sent()
            .iter()
            .filter(|message| {
                message.get("method") == Some(&Value::String("tools/call".to_owned()))
            })
            .filter_map(|message| {
                let params = message.get("params")?;
                let Some(Value::String(name)) = params.get("name") else {
                    return None;
                };
                Some((name.clone(), params.get("arguments")?.clone()))
            })
            .collect()
    }

    pub(crate) fn names(&self) -> Vec<String> {
        self.calls().into_iter().map(|(name, _)| name).collect()
    }

    pub(crate) fn channel(&self) -> Box<dyn Channel> {
        Box::new(self.clone())
    }
}

impl Channel for Script {
    fn send<'a>(
        &'a mut self,
        data: &'a [u8],
        deadline: Instant,
    ) -> BoxFuture<'a, Result<(), ChannelLost>> {
        Box::pin(async move {
            let text = std::str::from_utf8(data).map_err(|_| ChannelLost)?;
            let message = json(text.trim_end_matches('\n'));
            let delay = {
                let mut inner = self.inner();
                let count = inner.sent.len();
                inner
                    .send_delay
                    .as_mut()
                    .map_or(Duration::ZERO, |delay| delay(&message, count, deadline))
            };
            sleep(delay).await;
            let mut inner = self.inner();
            if inner
                .refuse_containing
                .is_some_and(|refused| text.contains(refused))
            {
                return Err(ChannelLost);
            }
            inner.sent.push(message);
            inner.lines.push(text.to_owned());
            Ok(())
        })
    }

    fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        Box::pin(async move {
            assert_eq!(limit, crate::cua::MAX_MESSAGE_BYTES);
            let delay = {
                let mut inner = self.inner();
                inner.deadlines.push(deadline);
                if inner.broken {
                    return Err(ChannelLost);
                }
                inner.receive_delay
            };
            sleep(delay).await;
            let mut inner = self.inner();
            if let Some(raw) = inner.raw.pop_front() {
                return Ok(raw);
            }
            let id = inner
                .sent
                .last()
                .and_then(|request| request.get("id"))
                .cloned()
                .unwrap_or(Value::Null);
            match inner.replies.pop_front().ok_or(ChannelLost)? {
                Reply::Answer(Value::Object(members)) => {
                    let mut answer = vec![
                        ("jsonrpc".to_owned(), Value::String("2.0".to_owned())),
                        ("id".to_owned(), id),
                    ];
                    answer.extend(members);
                    let options = team_protocol::python::json::Options {
                        ensure_ascii: true,
                        sort_keys: false,
                        compact: false,
                    };
                    Ok(
                        team_protocol::python::json::dumps(&Value::Object(answer), options)
                            .into_bytes(),
                    )
                }
                Reply::Answer(_) => Err(ChannelLost),
            }
        })
    }
}

/// Padded standard base64, as `base64.b64encode` writes it.
pub(crate) fn base64(bytes: &[u8]) -> String {
    let alphabet = crate::cua::BASE64;
    let mut text = String::new();
    for chunk in bytes.chunks(3) {
        let group = chunk
            .iter()
            .enumerate()
            .fold(0_u32, |group, (index, byte)| {
                group | (u32::from(*byte) << (16 - 8 * index))
            });
        for index in 0..4 {
            if index <= chunk.len() {
                text.push(char::from(
                    alphabet[((group >> (18 - 6 * index)) & 0x3f) as usize],
                ));
            } else {
                text.push('=');
            }
        }
    }
    text
}
