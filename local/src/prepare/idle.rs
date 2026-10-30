//! `shimpz-team prepare-idle`: the preparation helper's entrypoint, which only waits for SIGTERM. Each file is
//! prepared by its own `shimpz-team prepare` exec; the controller removes the helper after its segment.

use std::process::ExitCode;

use tokio::signal::unix::{SignalKind, signal};

/// Waits for SIGTERM, then exits 0.
#[must_use]
pub fn main() -> ExitCode {
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .build()
    else {
        return ExitCode::from(71);
    };
    runtime.block_on(async {
        match signal(SignalKind::terminate()) {
            Ok(mut terminate) => {
                terminate.recv().await;
                ExitCode::SUCCESS
            }
            Err(_) => ExitCode::from(71),
        }
    })
}
