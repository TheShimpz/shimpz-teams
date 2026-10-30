//! A scripted Brain for tests (feature `testing`). Never compiled into a shipped binary.

use std::collections::VecDeque;
use std::sync::{Mutex, MutexGuard, PoisonError};

use strict_json::Value;
use team_core::future::BoxFuture;

use crate::brain::{BrainClient, BrainError, BrainOperation, BrainRequest};

/// One call the scripted Brain received.
#[derive(Clone, Debug, PartialEq)]
pub struct Received {
    /// Its operation.
    pub operation: BrainOperation,
    /// Its body.
    pub body: Value,
    /// The provider of its credential, when it carried one (never the key).
    pub provider: Option<String>,
}

/// A Brain that answers queued results in order and records every call. A call whose Stop is already set answers
/// [`BrainError::Stopped`] without consuming a result.
#[derive(Debug, Default)]
pub struct ScriptedBrain {
    answers: Mutex<VecDeque<Result<Value, BrainError>>>,
    received: Mutex<Vec<Received>>,
}

impl ScriptedBrain {
    fn answers(&self) -> MutexGuard<'_, VecDeque<Result<Value, BrainError>>> {
        self.answers.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Queues the next answer.
    pub fn answer(&self, answer: Result<Value, BrainError>) {
        self.answers().push_back(answer);
    }

    /// Every call received so far.
    #[must_use]
    pub fn received(&self) -> Vec<Received> {
        self.received
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

impl BrainClient for ScriptedBrain {
    fn call<'a>(&'a self, request: BrainRequest<'a>) -> BoxFuture<'a, Result<Value, BrainError>> {
        Box::pin(async move {
            if request.stop.is_stopped() {
                return Err(BrainError::Stopped);
            }
            let received = Received {
                operation: request.operation,
                body: request.body,
                provider: request
                    .credential
                    .map(|credential| credential.provider.clone()),
            };
            self.received
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push(received);
            self.answers()
                .pop_front()
                .unwrap_or(Err(BrainError::Unavailable))
        })
    }
}

#[cfg(test)]
mod tests {
    use strict_json::Value;
    use team_core::stop::Stop;
    use tokio::time::Instant;

    use super::ScriptedBrain;
    use crate::brain::{BrainClient, BrainError, BrainOperation, BrainRequest};

    fn request(stop: &Stop) -> BrainRequest<'_> {
        BrainRequest {
            operation: BrainOperation::Turn,
            body: Value::Null,
            credential: None,
            deadline: Instant::now(),
            stop,
        }
    }

    #[tokio::test]
    async fn answers_in_order_and_refuses_a_stopped_call() {
        let brain = ScriptedBrain::default();
        brain.answer(Ok(Value::Bool(true)));
        let stop = Stop::new();
        assert_eq!(brain.call(request(&stop)).await, Ok(Value::Bool(true)));
        assert_eq!(
            brain.call(request(&stop)).await,
            Err(BrainError::Unavailable)
        );
        stop.stop();
        assert_eq!(brain.call(request(&stop)).await, Err(BrainError::Stopped));
        assert_eq!(brain.received().len(), 2);
    }
}
