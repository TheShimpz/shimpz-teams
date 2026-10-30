use super::{MAX_PATTERN_PROGRAM, pattern_admitted, pattern_preparation, pattern_program};

const ADMITTED: &[&str] = &[
    "",
    "^[a-z0-9_-]+$",
    "[^a]",
    "[]a]",
    "[-a]",
    "[a-]",
    "\\d{2,4}?",
    "(?i)abc",
    "(?ims)a",
    "(?-i:a)(?s:.)",
    "(?P<name>x)",
    "(?P<_a_1>x)",
    "[\\.a]",
    "\\x41",
    "\\.\\%\\!\\_\\ ",
    "\\a\\f\\n\\r\\t\\v",
    "a|b|",
    "\\Aa\\b\\B$",
    "(?:)*",
    "(a+)+b",
    "a{1000}",
    "(?:a{100}){10}",
    "(?:(?:a{10}){10}){10}",
    "a{0}(?:b{2}){500}",
    "^[a-zA-Z0-9_-]{1,64}$",
    "^.{1,900}$",
];

const REFUSED: &[&str] = &[
    "(",
    "\\p{L}",
    "\\pL",
    "\\x{41}",
    "\\u{41}",
    "\\u0041",
    "\\U00000041",
    "(?<name>x)",
    "(?P<a.b>x)",
    "(?P<é>x)",
    "(?P<aé>x)",
    "[A-\\u{61}]",
    "[\\x{41}-z]",
    "a(?i)b",
    "(?x)^[a-z]+$",
    "(?ix)a",
    "(?x:a)",
    "(?-x:a)",
    "(?-i)a",
    "(?U)a*",
    "[a--b]",
    "[a&&b]",
    "[[a](]",
    "[[:alpha:]]",
    "[\\pL]",
    "[a-\\u{41}]",
    "[a-b-c]",
    "^*",
    "a**",
    "a{2}{3}",
    "a{1001}",
    "a{1001,}",
    "a{2,1001}",
    "(?:a{100}){11}",
    "(?:(?:a{10}){10}){11}",
    "(?:a{100}|b){11}",
    "(?:ba{100}){11}",
    "a{ 2}",
    "a{2 }",
    "a{1, 2}",
    "\\z",
    "\\b{start}",
    "\\<",
    "\\1",
    "(?=a)",
    // Each bound of the RE2 program exceeds 16,384 instructions.
    "^.{1,1000}$",
];

#[test]
fn patterns_are_limited_to_syntax_both_engines_read_alike() {
    for pattern in ADMITTED
        .iter()
        .map(|pattern| (*pattern).to_owned())
        .chain(["a".repeat(2046)])
    {
        assert!(pattern_admitted(&pattern), "{pattern}");
    }
    let generated = [
        "a".repeat(2047),
        format!("(?:{}){{1000}}", "\\W".repeat(20)),
        format!("{}a{}", "(".repeat(33), ")".repeat(33)),
    ];
    for pattern in REFUSED.iter().copied().map(str::to_owned).chain(generated) {
        assert!(!pattern_admitted(&pattern), "{pattern}");
    }
}

#[test]
fn program_bound_charges_each_class_by_its_members() {
    // An ASCII class costs 32 plus 4 per member, and a class with a non-ASCII member or a negated Perl class 128 plus
    // 32 per member.
    assert!(pattern_admitted("[a-z0-9]{399}"));
    assert!(!pattern_admitted("[a-z0-9]{400}"));
    assert!(pattern_admitted("[\\w]{442}"));
    assert!(!pattern_admitted("[\\w]{443}"));
    assert!(pattern_admitted("[é]{101}"));
    assert!(!pattern_admitted("[é]{102}"));
    assert!(pattern_admitted("[\\W]{101}"));
    assert!(!pattern_admitted("[\\W]{102}"));
    assert!(pattern_admitted("[éa-é]{1}"));
}

#[test]
fn preparation_charges_the_base_the_bound_and_every_class_fold() {
    let base = 1 << 16;
    let cases: [(&str, u64); 8] = [
        ("a", base + 256 * 24),
        // A literal, a range, and a Perl class cover one, their span, and the ASCII range of code points below U+20000.
        ("[a]", base + 256 * (16 + 36) + 64),
        ("[a-z\\d]", base + 256 * (16 + 40) + 64 * (26 + 128)),
        ("(?i:[A-\u{ffff}])", base + 256 * (16 + 162) + 64 * 0xFFBF),
        // Code points at or above U+20000 are only scanned, and a negated Perl class covers every code point.
        (
            "[\u{20000}-\u{2ffff}]",
            base + 256 * (16 + 160) + 4 * 0x1_0000,
        ),
        (
            "[\\Wa]",
            base + 256 * (16 + 192) + 64 * 0x2_0000 + 4 * 0xF_0000 + 64,
        ),
        // A repetition copies a class into the program but folds it once.
        (
            "[a]{10}|[a]",
            base + 256 * (16 + 10 * 37 + 1 + 36 + 2) + 2 * 64,
        ),
        ("(?:[a])", base + 256 * (16 + 38) + 64),
    ];
    for (pattern, preparation) in cases {
        assert_eq!(pattern_preparation(pattern), Some(preparation), "{pattern}");
    }
    // A refused pattern, whatever its classes, has no preparation.
    for refused in ["[[:alpha:]]", "[a&&b]", "\\p{L}", "[[a]]"] {
        assert_eq!(pattern_preparation(refused), None, "{refused}");
    }
}

#[test]
fn program_bound_counts_the_search_overhead_and_every_node() {
    assert_eq!(pattern_program(""), Some(17));
    assert_eq!(pattern_program("(?i)"), Some(16));
    assert_eq!(pattern_program("[a-z]{100}"), Some(3717));
    assert_eq!(pattern_program("a|"), Some(16 + 2 + 8 + 1));
    assert_eq!(pattern_program("(a)*"), Some(16 + 8 + 2 + 2));
    assert_eq!(pattern_program("\\b."), Some(16 + 2 + 16));
    assert_eq!(pattern_program("\\d"), Some(16 + 32));
    assert_eq!(
        pattern_program(&format!("(?i){}x", "ж".repeat(1000))),
        Some(16 + 8 * 1001)
    );
    assert_eq!(
        pattern_program(&"a".repeat(2046)),
        Some(MAX_PATTERN_PROGRAM)
    );
}
