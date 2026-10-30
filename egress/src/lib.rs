//! Team egress policy (ADR-0123, plan phase R7, workstream W1): each Assistant's egress token and policy file under
//! the shared policy directory, with their custody, canonical bytes, and durable writes. The enforcement proxy stays in
//! the Assistant domain. It replaces `egress/policy.py`.
