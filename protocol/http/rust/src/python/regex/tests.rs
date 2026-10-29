use super::{MatchError, Pattern, TranslateError, declared, translate};

fn full(pattern: &str, text: &str) -> bool {
    Pattern::new(pattern)
        .expect("a translatable pattern")
        .is_full(text)
}

fn found(pattern: &str, text: &str) -> Option<(usize, usize)> {
    let found = Pattern::new(pattern)
        .expect("a translatable pattern")
        .search(text);
    found
        .expect("a completed search")
        .map(|found| (found.start, found.end))
}

/// `(pattern, text, fullmatch)` with Python's answers.
const FULL: [(&str, &str, bool); 52] = [
    (r"\w+", "é٣_ж", true),
    (r"\w", "-", false),
    (r"\w", "\u{300}", false),
    (r"\W", "-", true),
    (r"\d", "٣", true),
    (r"\d", "a", false),
    (r"\D", "a", true),
    (r"\s", "\u{1c}", true),
    (r"\s", "\u{85}", true),
    (r"\s", "\u{200b}", false),
    (r"\S", "a", true),
    (r"\S", " ", false),
    (r"[\w-]+", "a-é", true),
    (r"[\W]", "-", true),
    (r"[\W]", "a", false),
    (r"[\d\s]+", "1 ٣", true),
    (r"[\D]", "a", true),
    (r"[\S]", "a", true),
    (r"a$", "a\n", false),
    (r"\B", "", true),
    (r"a{2}", "aa", true),
    (r"a{,2}", "aa", true),
    (r"a{1,}", "aaa", true),
    (r"a{,}", "", true),
    (r"a{", "a{", true),
    (r"a{x}", "a{x}", true),
    (r"{}", "{}", true),
    (r"a{1", "a{1", true),
    (r"\n\t\r\f\v\a", "\n\t\r\u{c}\u{b}\u{7}", true),
    (r"\x41\u00e9\U0001F600", "Aé😀", true),
    (r"\.\-#&~ ", ".-#&~ ", true),
    (r"(?:a|b)(c)?(?!e)", "a", true),
    (r"a(?=b)b(?<=a|b)", "ab", true),
    (r"(?<!x)y", "y", true),
    (r"a.c", "abc", true),
    (r"a.c", "a\nc", false),
    (r"[]a]+", "]a", true),
    (r"[^]a]", "b", true),
    (r"[a-c-]+", "b-", true),
    (r"[\-\]\b]+", "-]\u{8}", true),
    (r"[[&~|]+", "[&~|", true),
    (r"[\x41-\x43]", "B", true),
    (r"[\u00e8-\u00e9]", "é", true),
    (r"\ud800", "a", false),
    (r"[^\ud800-\udfff]", "\n", true),
    (r"[\ud800-\udfff]", "a", false),
    (r"[\ud7ff-\ue000]", "\u{d7ff}", true),
    (r"[\ud7ff-\ue000]", "\u{e000}", true),
    (r"[a\ud800]", "a", true),
    (r"[a-a]", "a", true),
    (r"[\U0010ffff]", "\u{10ffff}", true),
    (r"[a-\U0010ffff]", "\u{10ffff}", true),
];

/// A byte span.
type Span = Option<(usize, usize)>;

/// `(pattern, text, the span search finds)` with Python's answers, in bytes.
const FOUND: [(&str, &str, Span); 9] = [
    (r"a$", "a\n", Some((0, 1))),
    (r"a$", "a\nb", None),
    (r"a\Z", "a\n", None),
    (r"\Aa", "ba", None),
    (r"\bé", "aé", None),
    (r"\bé", " é", Some((1, 3))),
    (r"a\B", "ab", Some((0, 1))),
    (r"^x", "x", Some((0, 1))),
    (r"\d", "a", None),
];

#[test]
fn patterns_have_python_meaning() {
    for (pattern, text, expected) in FULL {
        assert_eq!(full(pattern, text), expected, "{pattern} {text:?}");
    }
    for (pattern, text, expected) in FOUND {
        assert_eq!(found(pattern, text), expected, "{pattern} {text:?}");
    }
}

#[test]
fn unsupported_syntax_is_refused() {
    for (pattern, error) in [
        (r"\1", TranslateError::Unsupported),
        (r"\q", TranslateError::Unsupported),
        (r"\xZZ", TranslateError::Unsupported),
        (r"\U00110000", TranslateError::Unsupported),
        (r"[\U00110000]", TranslateError::Unsupported),
        (r"[a-\U00110000]", TranslateError::Unsupported),
        (r"(?i)a", TranslateError::Unsupported),
        (r"(?P<n>a)", TranslateError::Unsupported),
        (r"\", TranslateError::Truncated),
        (r"(?", TranslateError::Unsupported),
        (r"[a", TranslateError::Truncated),
        (r"\x4", TranslateError::Truncated),
        (r"[b-a]", TranslateError::BadRange),
        (r"[\w-a]", TranslateError::BadRange),
        (r"[a-\w]", TranslateError::BadRange),
        (r"(", TranslateError::Engine),
    ] {
        assert_eq!(
            translate(pattern).and_then(|_| Pattern::new(pattern).map(|_| ())),
            Err(error),
            "{pattern}"
        );
    }
    assert!(translate(r"(?=a)").is_ok());
}

#[test]
fn iteration_and_substitution_follow_python() {
    let pattern = Pattern::new(r"(\d)(x)?").expect("pattern");
    let found = pattern.finditer("1x2").expect("completed");
    let groups: Vec<_> = found
        .iter()
        .map(|one| (one.group("1x2", 1), one.group("1x2", 2)))
        .collect();
    assert_eq!(groups, [(Some("1"), Some("x")), (Some("2"), None)]);
    assert_eq!(
        pattern.sub("a1x2b", |text| format!("<{text}>")),
        Ok("a<1x><2>b".to_owned())
    );
    let empty = Pattern::new(r"a*").expect("pattern");
    assert_eq!(empty.finditer("b"), Err(MatchError::EmptyMatch));
    assert_eq!(empty.sub("b", str::to_owned), Err(MatchError::EmptyMatch));
}

#[test]
fn convenience_checks_refuse_on_any_doubt() {
    let checks = [
        declared(r"\d").is_absent("a"),
        !declared(r"\d").is_absent("1"),
        declared(r"a").is_full_value(&strict_json::Value::String("a".to_owned())),
        !declared(r"a").is_full_value(&strict_json::Value::Null),
    ];
    assert_eq!(checks, [true; 4]);
}

#[test]
fn backtracking_exhaustion_refuses_the_operation() {
    let pattern = Pattern::with_limit(r"(?:(?=a)a|a)*b", 100_000).expect("pattern");
    let text = format!("{}c", "a".repeat(40));
    assert_eq!(Pattern::new(r"(?:(?=a)a|a)*b").map(|_| ()), Ok(()));
    assert_eq!(pattern.fullmatch(&text), Err(MatchError::Engine));
    assert_eq!(pattern.search(&text), Err(MatchError::Engine));
    assert_eq!(pattern.finditer(&text), Err(MatchError::Engine));
    assert!(!pattern.is_full(&text));
    assert!(!pattern.is_absent(&text));
}
