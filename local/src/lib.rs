//! The Local Team controller (ADR-0123, plan phase R7): the `shimpz-team` binary's library.
//!
//! This crate is the only one that reaches Docker or serves HTTP. Its modules are owned by the R7 workstreams:
//! [`cli`], [`authority`], and [`http`] (W8, the route table frozen in W0), [`coordination`] (frozen in W0: the one
//! authority that serializes Team mutation, chat admission, cancellation, preview draining, and teardown), [`docker`]
//! (W4a), the [`prepare`] seam (W4b), [`layout`], and [`seams`], the composition root's injection points.

pub mod audit;
pub mod authority;
pub mod browser;
pub mod cli;
pub mod coordination;
pub mod docker;
pub mod http;
pub mod layout;
pub mod prepare;
pub mod seams;
#[cfg(feature = "testing")]
pub mod testing;
