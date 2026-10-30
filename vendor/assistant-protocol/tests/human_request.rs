//! The published human-request vectors: fingerprints, requests, and replay transcripts.

mod common;

use assistant_protocol::catalog::{catalog_by_id, catalog_error};
use assistant_protocol::human_request::{fingerprint, request_error, transcript_error};
use assistant_protocol::python::array;

#[test]
fn every_human_request_vector_agrees() {
    let document = common::vectors("human-request.json");
    let section = common::field(&document, "catalog");
    let messages = common::field(section, "messages");
    assert_eq!(
        catalog_error(messages, common::field(section, "summary")),
        None
    );
    let catalog = catalog_by_id(messages);
    let fingerprints = common::field(&document, "fingerprint");
    for case in array(common::field(fingerprints, "cases")).expect("cases") {
        assert_eq!(
            fingerprint(common::field(case, "request")),
            common::text(case, "sha256"),
            "{}",
            common::name(case)
        );
    }
    for case in array(common::field(&document, "request_cases")).expect("cases") {
        let actual = request_error(common::field(case, "request"), &catalog);
        assert_eq!(actual, common::expected(case), "{}", common::name(case));
    }
    for case in array(common::field(&document, "transcript_cases")).expect("cases") {
        let actual = transcript_error(
            common::field(case, "requests"),
            common::field(case, "responses"),
            &catalog,
        );
        assert_eq!(actual, common::expected(case), "{}", common::name(case));
    }
}
