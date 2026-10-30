//! The published failure envelope vectors.

mod common;

use assistant_protocol::failure::failure_error;

#[test]
fn every_failure_vector_agrees() {
    common::agree("failure.json", |case| {
        failure_error(common::field(case, "response"))
    });
}
