//! A chat turn as a durable handoff between Admin and Team (ADR-0125; plan B0 and B3): its send recorded before it
//! runs, its checkpoints after every Brain answer, its terminal kept until Admin's history committed it, and its
//! authority bound to the session and the grant it was sent under.
//!
//! A send of a turn this process runs attaches to it; one of a turn that ended receives its kept terminal; one of a
//! turn a previous Team process left running resumes it from its last checkpoint, under the same session and body
//! only. A turn whose grant ends while it runs is stopped, so no turn outlives its authority while Admin cannot stop
//! it. Admin settles a turn as delivered once its history committed the terminal, or as revoked once its session
//! ended, which stops it here.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use strict_json::Value;
use team_chat::orchestrator::{ChatContinuation, ChatError};
use team_chat::progress::Reporter;
use team_inference::memory::Source;
use team_inference::usage::TurnUsage;
use team_protocol::payload::canonical_request_identity;
use team_protocol::python::value::{object_of, string};
use tokio::sync::watch;

use super::super::continuation::{checkpoint, checkpoint_payload};
use super::super::inference::team_id;
use super::super::pending::PendingLocalChat;
use super::super::service::{ChatService, stream_terminal, terminal_of};
use super::store::{Claim, Live, Sender, Terminal, Turns};
use super::{ChatBody, Request, chat_body};
use crate::http::service::{Answer, Body, Call, Principal, Problem};

/// One durable turn as its segments see it: its hold, and what its checkpoints carry beside the continuation.
pub struct Durable {
    turns: Arc<Turns>,
    live: Live,
    started: Mutex<Option<(Option<Source>, TurnUsage)>>,
}

impl std::fmt::Debug for Durable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Durable")
    }
}

impl Durable {
    /// Records the turn's author and when it was admitted, which every checkpoint keeps.
    pub fn started(&self, source: Option<Source>, usage: TurnUsage) {
        *self.started.lock().unwrap_or_else(PoisonError::into_inner) = Some((source, usage));
    }

    /// Makes `pending` the turn's checkpoint, after proving it decodes again: a checkpoint a restart could not read
    /// would leave the turn unable to resume.
    ///
    /// # Errors
    /// [`ChatError`] when it does not round-trip or could not be made durable.
    pub fn checkpoint(&self, mut pending: PendingLocalChat) -> Result<(), ChatError> {
        let started = self
            .started
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        if let Some((source, usage)) = started {
            (pending.source, pending.usage) = (source, Some(usage));
        }
        let payload = checkpoint_payload(&pending);
        if checkpoint(&payload).is_err() {
            return Err(ChatError::Orchestration(
                "chat turn checkpoint does not round-trip",
            ));
        }
        self.turns
            .checkpoint(&self.live, payload)
            .map_err(|_| ChatError::Orchestration("chat turn checkpoint is unavailable"))
    }
}

/// The checkpoint of a turn's state after a Brain answer, with what its segment selected and ran on.
#[must_use]
pub fn pending_at(
    continuation: &ChatContinuation,
    selection: (&[String], &[String]),
    identity: super::super::pending::Identity,
    (provider, model, locale): (&str, &str, Option<&String>),
) -> PendingLocalChat {
    let mut assistant_ids = selection.0.to_vec();
    assistant_ids.sort_unstable();
    PendingLocalChat {
        continuation: continuation.clone(),
        assistant_ids,
        file_ids: selection.1.to_vec(),
        provider: Some(provider.to_owned()),
        identity,
        transcripts: Vec::new(),
        requests_used: 0,
        locale: locale.cloned(),
        usage: None,
        paused_batch: None,
        recording: None,
        model: Some(model.to_owned()),
        source: None,
    }
}

/// A turn this process runs, as a later send of it receives it: its terminal once it ends.
async fn attached(mut receiver: watch::Receiver<Option<Terminal>>) -> Terminal {
    loop {
        if let Some(terminal) = receiver.borrow_and_update().clone() {
            return terminal;
        }
        if receiver.changed().await.is_err() {
            // The turn's run ended without its terminal: a later send resumes it.
            return terminal_of(Err(Problem::new(
                503,
                "the chat turn was interrupted",
                "turn-interrupted",
            )));
        }
    }
}

fn authority(call: &Call<'_>) -> Result<String, Problem> {
    match &call.principal {
        Principal::Supervisor {
            authority_sha256, ..
        } => Ok(authority_sha256.clone()),
        _ => Err(Problem::new(
            403,
            "a chat turn needs a Supervisor session",
            "turn-authority",
        )),
    }
}

/// How long until the wall-clock second `at`, from `now` milliseconds.
fn until(at: i64, now_ms: i64) -> Duration {
    let left = at.saturating_mul(1000).saturating_sub(now_ms);
    Duration::from_millis(u64::try_from(left).unwrap_or(0))
}

impl ChatService {
    /// Admits one send of a turn: the turn is recorded before it runs, and its progress and terminal are streamed.
    pub(in crate::chat) fn start(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        let request = self.request(call)?;
        let body = chat_body(&call.body)?;
        let Body::Json(raw) = &call.body else {
            return Err(Problem::new(
                422,
                "Team chat requires a JSON body",
                "invalid-body",
            ));
        };
        let sender = Sender::of(&body.nonce, body.issued_at, &authority(call)?, raw);
        let claim = self
            .parts
            .turns
            .claim(&request.team_id, &sender, self.now_ms() / 1000)?;
        let service = self.clone();
        Ok(stream_terminal(move |reporter| async move {
            service
                .durable(request, body, &sender, claim, reporter)
                .await
        }))
    }

    async fn durable(
        &self,
        request: Request,
        body: ChatBody,
        sender: &Sender,
        claim: Claim,
        progress: Reporter,
    ) -> Terminal {
        let (live, resumed) = match claim {
            Claim::Ended(terminal) => return terminal,
            Claim::Attached(receiver) => return attached(receiver).await,
            Claim::New(live) => (live, None),
            Claim::Orphaned(live, checkpoint) => (live, checkpoint),
        };
        let team_id = request.team_id.clone();
        let durable = Arc::new(Durable {
            turns: Arc::clone(&self.parts.turns),
            live,
            started: Mutex::new(None),
        });
        let work = self.durable_work(request, body, resumed, Arc::clone(&durable), progress);
        let mut work = std::pin::pin!(work);
        let grant = until(sender.grant_deadline(), self.now_ms());
        let result = tokio::select! {
            result = &mut work => result,
            () = tokio::time::sleep(grant) => {
                // The turn's authority ended while it ran: it stops, and its ending is what Admin receives.
                let _ = self.parts.coordination.executions.cancel(&team_id, true);
                work.await
            }
        };
        let terminal = terminal_of(result);
        // A terminal that could not be recorded is still streamed; the turn stays running at its last checkpoint, so a
        // later send of it resumes from there.
        let _ = self.parts.turns.end(&durable.live, &terminal);
        terminal
    }

    /// A new turn from its start, or one a previous Team process left, from its last checkpoint.
    async fn durable_work(
        &self,
        request: Request,
        body: ChatBody,
        resumed: Option<Value>,
        durable: Arc<Durable>,
        progress: Reporter,
    ) -> Result<Value, Problem> {
        let Some(saved) = resumed else {
            return self.turn(request, body, progress, Some(durable)).await;
        };
        let pending = checkpoint(&saved)
            .map_err(|_| Problem::new(503, "the chat turn cannot resume", "turn-unresumable"))?;
        let admitted = self.admit(&request.team_id)?;
        if let (source, Some(usage)) = (pending.source.clone(), pending.usage.clone()) {
            durable.started(source, usage);
        }
        self.continue_paused(
            &admitted,
            request,
            pending,
            (Vec::new(), 0),
            progress,
            Some(durable),
        )
        .await
    }

    /// Settles one turn of the Team: `delivered` once Admin's history committed its terminal, or `revoked` once its
    /// session ended, which stops it if it still runs here.
    pub(in crate::chat) fn settle(&self, call: &Call<'_>, revoked: bool) -> Result<Value, Problem> {
        let team_id = team_id(call)?;
        let shape = Problem::new(
            422,
            "a chat turn settles by exactly its request",
            "invalid-body",
        );
        let identity = match &call.body {
            Body::Json(body @ Value::Object(members)) if members.len() == 1 => body
                .get("request")
                .and_then(canonical_request_identity)
                .ok_or(shape)?,
            _ => return Err(shape),
        };
        let nonce = identity.get("nonce").and_then(string).unwrap_or_default();
        let (running, saved) = self.parts.turns.settle(team_id, nonce, &authority(call)?)?;
        if revoked && running {
            let _ = self.parts.coordination.executions.cancel(team_id, true);
        }
        // A turn settled while it waited on its Browser task leaves that task to nobody: it is discarded.
        let browser = saved
            .and_then(|saved| checkpoint(&saved).ok())
            .and_then(|pending| pending.continuation.turn.browser);
        if let (Some(request), Some(service)) = (browser, &self.parts.browser) {
            service.discard(team_id, &request.interrupt_id);
        }
        Ok(object_of(vec![
            ("team_id", Value::String(team_id.to_owned())),
            ("settled", Value::Bool(true)),
        ]))
    }
}
