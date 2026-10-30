//! An NDJSON answer: advisory progress records, then one authoritative terminal record carrying the operation's
//! status and answer.
//!
//! The pump drains the producer to its end even when the client is gone, so a disconnected consumer never changes the
//! admitted operation. A record that violates the progress protocol, or a write that fails, stops every later write;
//! the connection then ends without the terminating chunk, so the consumer fails closed.

use std::sync::Arc;

use hyper::body::{Bytes, Frame};
use strict_json::Value;
use team_protocol::progress::encode_record;
use team_protocol::python::value::int;
use tokio::sync::{OwnedSemaphorePermit, mpsc};

use super::audit::RequestAudit;
use super::respond::Frames;
use crate::http::activity::{Activity, OwnedWorking};
use crate::http::service::StreamRecord;

/// The status codes of a streamed operation that answered rather than failed: success, and a turn paused for a
/// person (`428`).
fn answered(status: u16) -> bool {
    status < 400 || status == 428
}

/// The pump of one streamed answer.
pub struct Pump {
    /// The request's audit state.
    pub audit: RequestAudit,
    /// The body's frames.
    pub frames: Frames,
    /// The request's unit of in-flight work, held until the terminal record.
    pub working: Option<OwnedWorking>,
    /// The activity a Supervisor's mutating stream marks when it ends.
    pub supervised: Option<Arc<Activity>>,
    /// The connection's admission slot, held until the operation ends.
    pub slot: Arc<OwnedSemaphorePermit>,
}

impl Pump {
    /// Writes each record as it comes, then the terminal one after recording the outcome.
    pub async fn run(self, mut records: mpsc::Receiver<StreamRecord>) {
        let mut writable = true;
        let (status, body) = loop {
            match records.recv().await {
                Some(StreamRecord::Progress(event)) => {
                    if writable {
                        writable = self.write(progress(event)).await;
                    }
                }
                Some(StreamRecord::Terminal { status, body }) => break (status, body),
                None => {
                    let failure = vec![(
                        "error".to_owned(),
                        Value::String("internal error".to_owned()),
                    )];
                    break (500, Value::Object(failure));
                }
            }
        };
        let terminal = self.terminal(status, body).await;
        writable = writable && terminal.is_some() && self.write(terminal).await;
        if !writable {
            // End without the terminating chunk: the consumer must not read a complete stream.
            let _ = self
                .frames
                .send(Err(std::io::ErrorKind::BrokenPipe.into()))
                .await;
        }
        if let Some(activity) = &self.supervised
            && self.audit.principal_class == "human"
        {
            activity.supervised();
        }
        self.audit.end();
        drop(self.working);
        drop(self.slot);
    }

    /// Records the outcome and builds the terminal record, or `None` when the journal could not record it.
    async fn terminal(&self, status: u16, body: Value) -> Option<Value> {
        let Value::Object(mut members) = body else {
            return None;
        };
        let (result, detail) = if answered(status) {
            ("ok", None)
        } else {
            let code = members
                .iter()
                .find(|(name, _)| name == "code")
                .and_then(|(_, code)| match code {
                    Value::String(code) => Some(code.clone()),
                    _ => None,
                })
                .unwrap_or_else(|| "internal-error".to_owned());
            (if status < 500 { "denied" } else { "error" }, Some(code))
        };
        self.audit
            .record(None, result, None, detail.as_deref())
            .await
            .ok()?;
        members.retain(|(name, _)| name != "trace_id");
        members.push((
            "trace_id".to_owned(),
            Value::String(self.audit.trace_id.clone()),
        ));
        Some(Value::Object(vec![
            ("type".to_owned(), Value::String("terminal".to_owned())),
            ("status".to_owned(), int(status.into())),
            ("body".to_owned(), Value::Object(members)),
        ]))
    }

    /// Writes one record as one chunk; `false` once the record is not canonical or the client is gone.
    async fn write(&self, record: Option<Value>) -> bool {
        let Some(encoded) = record.and_then(|record| encode_record(&record).ok()) else {
            return false;
        };
        self.frames
            .send(Ok(Frame::data(Bytes::from(encoded))))
            .await
            .is_ok()
    }
}

/// `{"type": "progress", **event}`, or `None` for an event that is no object.
fn progress(event: Value) -> Option<Value> {
    let Value::Object(members) = event else {
        return None;
    };
    let mut record = vec![("type".to_owned(), Value::String("progress".to_owned()))];
    record.extend(members.into_iter().filter(|(name, _)| name != "type"));
    Some(Value::Object(record))
}
