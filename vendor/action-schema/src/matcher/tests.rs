use super::{Budget, PatternError, pattern_matches};
use crate::{MAX_PATTERN_PROGRAM, MAX_PATTERN_WORK, pattern_preparation};

fn preparation(pattern: &str) -> u64 {
    pattern_preparation(pattern).expect("an admitted pattern")
}

fn matches(pattern: &str, subject: &str) -> Result<bool, PatternError> {
    pattern_matches(pattern, subject, &mut Budget::default())
}

#[test]
fn perl_classes_and_boundaries_are_ascii() {
    let cases = [
        ("^\\d$", "7", true),
        ("^\\d$", "\u{663}", false),
        ("^\\D$", "\u{663}", true),
        ("^\\w$", "é", false),
        ("^\\W$", "é", true),
        ("^\\s$", "\u{a0}", false),
        ("^\\S$", "\u{a0}", true),
        ("^[\\d]$", "5", true),
        ("^[\\d]$", "\u{663}", false),
        ("^[\\w]$", "é", false),
        ("^[\\s]$", "\u{a0}", false),
        ("^[^\\w]$", "é", true),
        ("^[^\\d]$", "5", false),
        ("^[\\D]$", "é", true),
        ("^[\\w\\s]+$", "a b", true),
        ("^[\\W\\S]$", "é", true),
        ("\\bé", "é", false),
        ("a\\b", "a", true),
        ("é\\Bé", "éé", true),
        ("^\\B", "é", true),
        ("(?i)k", "\u{212a}", true),
    ];
    for (pattern, subject, expected) in cases {
        assert_eq!(
            matches(pattern, subject),
            Ok(expected),
            "{pattern} {subject}"
        );
    }
}

#[test]
fn a_refused_pattern_never_searches() {
    let mut budget = Budget::default();
    assert_eq!(
        pattern_matches("\\p{L}", "a", &mut budget),
        Err(PatternError::Refused)
    );
    assert_eq!(budget.remaining(), MAX_PATTERN_WORK);
}

#[test]
fn preparation_and_every_search_charge_the_program_bound() {
    let prepared = preparation("a");
    let mut budget = Budget::default();
    assert_eq!(pattern_matches("a", "éa", &mut budget), Ok(true));
    assert_eq!(budget.remaining(), MAX_PATTERN_WORK - prepared - 3 * 24);
    // An empty subject charges as one byte, and the prepared pattern is not charged again.
    assert_eq!(pattern_matches("a", "", &mut budget), Ok(false));
    assert_eq!(budget.remaining(), MAX_PATTERN_WORK - prepared - 4 * 24);
    assert_eq!(pattern_matches("a", "b", &mut budget), Ok(false));
    assert_eq!(budget.remaining(), MAX_PATTERN_WORK - prepared - 5 * 24);
    // Another pattern is prepared, and charged, before its first search.
    assert_eq!(pattern_matches("b", "b", &mut budget), Ok(true));
    assert_eq!(budget.remaining(), MAX_PATTERN_WORK - 2 * prepared - 6 * 24);
}

#[test]
fn a_charge_beyond_the_remaining_work_is_refused_before_it_runs() {
    let longest = "a".repeat(2046);
    let prepared = preparation(&longest);
    let units = usize::try_from((MAX_PATTERN_WORK - prepared) / MAX_PATTERN_PROGRAM).expect("fits");
    let mut budget = Budget::default();
    assert_eq!(
        pattern_matches(&longest, &"a".repeat(units), &mut budget),
        Ok(true)
    );
    assert_eq!(budget.remaining(), 0);
    assert_eq!(
        pattern_matches(&longest, "", &mut budget),
        Err(PatternError::Exhausted)
    );
    let mut fresh = Budget::default();
    assert_eq!(
        pattern_matches(&longest, &"a".repeat(units + 1), &mut fresh),
        Err(PatternError::Exhausted)
    );
    // The preparation was charged; the refused search was not.
    assert_eq!(fresh.remaining(), MAX_PATTERN_WORK - prepared);
    // A preparation beyond what remains is refused before the pattern compiles.
    let prepared_a = preparation("a");
    let mut spent = Budget::default();
    assert_eq!(
        pattern_matches(
            "a",
            &"a".repeat(usize::try_from((MAX_PATTERN_WORK - prepared_a) / 24).expect("fits")),
            &mut spent
        ),
        Ok(true)
    );
    assert_eq!(
        pattern_matches("b", "b", &mut spent),
        Err(PatternError::Exhausted)
    );
}
