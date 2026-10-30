//! The Routine journeys on fakes: claims and replayed runs, recovery and incidents, frozen runs and their answers,
//! recovery cards, the watchdog, teardown, and the HTTP routes.

mod cards;
mod frozen;
mod lifecycle;
mod recovery;
mod runs;

use team_storage::private::epoch_seconds;

/// Now, as the service reads it.
fn now() -> i64 {
    epoch_seconds(&team_core::clock::SystemClock)
}
