//! Team chat (ADR-0123, plan phase R7, workstream W6): the profile-neutral turn engine, its orchestration over the
//! Brain and Actions, its metadata-only progress, and the attachment contract. It replaces `chat/`.

pub mod attachments;
pub mod contract;
pub mod orchestrator;
pub mod progress;
pub mod turn;
