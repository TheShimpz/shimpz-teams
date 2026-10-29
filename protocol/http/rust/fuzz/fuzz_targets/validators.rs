//! Fuzzes every registered validator and encoder over arbitrary bytes decoded by `strict_json::parse`: each must
//! answer, refuse, or return its closed error, never panic or overflow the stack.
#![no_main]

// The registry calls the crate under test through these modules, which cargo-machete cannot follow.
use team_protocol as _;

#[path = "../../tests/protocol/support/registry/mod.rs"]
mod registry;
#[path = "../../tests/protocol/support/repr.rs"]
mod repr;

libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    if let Ok(value) = strict_json::parse(data) {
        for (name, call) in registry::all() {
            let outcome = call(&value);
            assert!(!registry::line(&outcome).is_empty(), "{name}");
            assert!(registry::find(name).is_some());
        }
    }
});
