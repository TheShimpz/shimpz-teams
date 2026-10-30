//! The Cua Driver's MCP answers: any lines, answering a session's opening, an observation with its screenshot, and an
//! action, are admitted or refused as closed failures, never a panic, and an observation stays within its bounds.

#![no_main]

use std::collections::VecDeque;
use std::time::Duration;

use libfuzzer_sys::fuzz_target;
use team_browser::cua::{Channel, ChannelLost, CuaSession, MAX_OBSERVATION_BYTES, MAX_REFS};
use team_core::future::BoxFuture;
use tokio::time::Instant;

/// A Driver whose answers are the fuzzed lines, in order; it then closes.
struct Lines(VecDeque<Vec<u8>>);

impl Channel for Lines {
    fn send<'a>(&'a mut self, _data: &'a [u8], _deadline: Instant) -> BoxFuture<'a, Result<(), ChannelLost>> {
        Box::pin(async { Ok(()) })
    }

    fn receive_line(&mut self, _deadline: Instant, limit: usize) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        Box::pin(async move {
            match self.0.pop_front() {
                Some(line) if line.len() <= limit => Ok(line),
                _ => Err(ChannelLost),
            }
        })
    }
}

fuzz_target!(|data: &[u8]| {
    let lines = data.split(|byte| *byte == b'\n').map(<[u8]>::to_vec).collect();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .start_paused(true)
        .build()
        .expect("a runtime");
    runtime.block_on(async {
        let mut session = CuaSession::new(Box::new(Lines(lines)));
        session.set_deadline(Some(Instant::now() + Duration::from_secs(600)));
        if session.open().await.is_err() {
            return;
        }
        if let Ok(observation) = session.observe(true).await {
            assert!(observation.text.len() <= MAX_OBSERVATION_BYTES);
            assert!(observation.refs.len() <= MAX_REFS);
        }
        let _ = session.click("p1:1").await;
    });
});
