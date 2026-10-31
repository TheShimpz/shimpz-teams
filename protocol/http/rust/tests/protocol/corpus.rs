//! The recorded corpus: per module, each entry's seeds and the drawn cases that executed a new line arc of the retired
//! Python module, with the line it wrote, replayed against the crate.

use std::path::Path;

use crate::support::registry::{find, line};
use crate::support::vectors::{items, text};

#[test]
fn every_corpus_case_matches_the_python_line() {
    let directory = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/protocol/corpus");
    let mut files: Vec<_> = std::fs::read_dir(&directory)
        .expect("the corpus directory")
        .map(|entry| entry.expect("a corpus file").path())
        .collect();
    files.sort();
    let mut replayed = 0;
    for file in files {
        let corpus = strict_json::parse(&std::fs::read(&file).expect("a readable corpus file"))
            .expect("a strict JSON corpus file");
        for case in items(&corpus) {
            let field = |key: &str| text(case.get(key).expect("a corpus field"));
            let call = find(field("name")).expect("a registered name");
            let input = strict_json::parse(field("input").as_bytes()).expect("a strict JSON input");
            assert_eq!(
                line(&call(&input)),
                field("line"),
                "{} {}",
                field("name"),
                field("input")
            );
            replayed += 1;
        }
    }
    assert!(replayed > 1000, "the corpus is present");
}
