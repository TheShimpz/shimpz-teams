//! A Browser exec's raw multiplexed output: any bytes are demultiplexed within their bounds without a panic, and
//! every line read is within its limit.

#![no_main]

use std::time::Duration;

use libfuzzer_sys::fuzz_target;
use team_local::browser::channel::Channel;
use tokio::io::AsyncWriteExt;
use tokio::time::Instant;

fuzz_target!(|data: &[u8]| {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .expect("a runtime");
    runtime.block_on(async {
        let (team, mut browser) = tokio::io::duplex(data.len().max(1));
        browser.write_all(data).await.expect("written");
        drop(browser);
        let mut channel = Channel::new(Box::new(team));
        let deadline = Instant::now() + Duration::from_secs(5);
        while let Ok(line) = channel.receive_line(deadline, 64).await {
            assert!(line.len() <= 64);
        }
        let _ = channel.receive_end(deadline).await;
    });
});
