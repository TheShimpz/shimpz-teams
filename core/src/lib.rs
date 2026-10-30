//! Team invariants that several Team responsibilities share (ADR-0123, plan phase R7).
//!
//! Only cohesive Team invariants live here: the wall clock and entropy every store and token reads through, the
//! `Stop` one turn or task observes, secret values that never print, the fallible audit sink every authority writes
//! before it acts, and the exec seam through which Team reaches a workload container. Docker itself stays in the
//! `local` crate; a profile-neutral crate reaches a workload only through [`exec::ExecEngine`].

pub mod audit;
pub mod clock;
pub mod entropy;
pub mod exec;
pub mod future;
pub mod secret;
pub mod stop;
#[cfg(feature = "testing")]
pub mod testing;
