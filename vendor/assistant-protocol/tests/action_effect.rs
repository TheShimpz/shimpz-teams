//! The published Action effect vectors.

mod common;

use assistant_protocol::action_effect::effect_error;

#[test]
fn every_action_effect_vector_agrees() {
    common::agree("action-effect.json", |case| {
        effect_error(common::field(case, "actions"))
    });
}
