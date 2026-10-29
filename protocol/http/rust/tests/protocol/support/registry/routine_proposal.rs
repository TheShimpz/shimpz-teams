//! `routine_proposal`.

use team_protocol::routine_proposal;

use super::{Entry, borrowed, value};

/// The entries of `routine_proposal`.
pub const ENTRIES: &[Entry] = &[
    ("routine_proposal.canonical_proposal", |v| {
        borrowed(routine_proposal::canonical_proposal(v))
    }),
    ("routine_proposal.canonical_refinement", |v| {
        borrowed(routine_proposal::canonical_refinement(v))
    }),
    ("routine_proposal.canonical_question", |v| {
        borrowed(routine_proposal::canonical_question(v))
    }),
    ("routine_proposal.canonical_refusal", |v| {
        value(routine_proposal::canonical_refusal(v))
    }),
    ("routine_proposal.canonical_proposal_answer", |v| {
        borrowed(routine_proposal::canonical_proposal_answer(v))
    }),
];
