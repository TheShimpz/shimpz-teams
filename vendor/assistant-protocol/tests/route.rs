//! The published route declaration and call-time match vectors.

mod common;

use assistant_protocol::route::{call_error, routes_error};

#[test]
fn every_route_declaration_vector_agrees() {
    common::agree("route.json", |case| {
        routes_error(common::field(case, "routes"))
    });
}

#[test]
fn every_route_match_vector_agrees() {
    common::agree("route-match.json", |case| {
        call_error(
            common::field(case, "routes"),
            common::text(case, "method"),
            common::text(case, "target"),
        )
    });
}
