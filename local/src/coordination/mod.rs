//! The one authority that coordinates Team mutation, chat admission, cancellation, preview draining, and teardown
//! (frozen in W0). Every service holds the same [`Coordination`]; none keeps its own lock of another's state.
//!
//! The order is fixed, as in the Python controller it replaces:
//!
//! - Team destruction: the names lock, then the Team's stripe, to confirm the name; then the Team's previews closed
//!   (every one ended and drained); then the Team's execution cancelled and its slot awaited (30 s); then the stripe
//!   again for the teardown itself, whose participants run in [`residue::DESTROY_ORDER`].
//! - Space reset: the names lock; then every execution drained (admission closed Space-wide, every one cancelled,
//!   every slot held, 30 s); then every preview closed; then all 64 stripes in index order; then the participants in
//!   [`residue::RESET_ORDER`].
//! - Chat never takes the names lock; an Assistant mutation refuses (`chat-active`) while the Team's slot is held.
//!
//! No lock here is re-entrant: a holder never takes the same lock again, and no lock is held across a Docker or Brain
//! call except where that ordering above says so.

pub mod executions;
pub mod locks;
pub mod previews;
pub mod residue;

use std::sync::Arc;

use executions::{ChatLimit, Executions};
use locks::SpaceLocks;
use previews::PreviewBook;

/// The shared coordination handles.
#[derive(Clone, Debug)]
pub struct Coordination {
    /// The names lock and the 64 Team stripes.
    pub locks: Arc<SpaceLocks>,
    /// Every Team's execution slot and the Space chat admission.
    pub executions: Executions,
    /// Every preview and download under way.
    pub previews: PreviewBook,
}

impl Coordination {
    /// Fresh handles; `limit` is the Space chat limit's store.
    #[must_use]
    pub fn new(limit: Arc<dyn ChatLimit>) -> Self {
        Self {
            locks: Arc::new(SpaceLocks::new()),
            executions: Executions::new(limit),
            previews: PreviewBook::new(),
        }
    }
}
