//! Team installation (ADR-0123, plan phase R7, workstream W3): resolving a publication from Developers through the
//! release proxy, proving its artifact (the recorded Sigstore bundles read by content address, the DSSE statements,
//! and the shipping cosign as a direct child process), the Team's publication bindings, icons, and automatic updates.
//! It replaces `install/` and the Developers client of `local/install/`.
//!
//! [`trust`] is the frozen seam: the controller installs through a [`trust::Resolver`] and a
//! [`trust::ArtifactVerifier`], which the live artifact-trust lane always runs for real.

pub mod bindings;
pub mod contract;
pub mod cosign;
pub mod developers;
pub mod icons;
pub mod lock;
pub mod release;
#[cfg(feature = "testing")]
pub mod testing;
pub mod trust;
pub mod update;
pub mod verifier;
