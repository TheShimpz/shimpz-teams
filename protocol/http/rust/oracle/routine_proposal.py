"""`routine_proposal`."""

from __future__ import annotations

import routine_proposal

from .common import cases

ENTRIES = {
    "routine_proposal.canonical_proposal": (routine_proposal.canonical_proposal, lambda: cases("routine_proposal")),
    "routine_proposal.canonical_refinement": (
        routine_proposal.canonical_refinement,
        lambda: cases("routine_refinement"),
    ),
    "routine_proposal.canonical_question": (routine_proposal.canonical_question, lambda: cases("routine_question")),
    "routine_proposal.canonical_refusal": (routine_proposal.canonical_refusal, lambda: cases("routine_refusal")),
    "routine_proposal.canonical_proposal_answer": (
        routine_proposal.canonical_proposal_answer,
        lambda: cases("routine_proposal_answer"),
    ),
}
