use super::{
    COMPILED, Compiled, DECIMAL_ZEROS, DIGITS, Negations, PhraseError, TZDATA_VERSION, ZONE_NAMES,
    lazy_finditer, loads, negation_starts, one_clause, outputs, outputs_in, overlaps, readings,
    requests_routine, requests_routine_in, sentences, stated, stated_in, team_asks, team_asks_in,
    translate_digits, zones, zones_by,
};
use crate::python::regex::{Match, MatchError, Pattern};
use crate::python::value::{int, object_of, text};
use strict_json::Value;

/// A pattern whose search exhausts the backtracking limit on [`exhausting_text`].
fn exhausting() -> Pattern {
    Pattern::with_limit(r"(?:(?=a)a|a)*b", SMALL_LIMIT).expect("pattern")
}

/// A backtracking limit small enough that a test text of moderate length exhausts it quickly.
const SMALL_LIMIT: usize = 100_000;

fn exhausting_text() -> String {
    format!("{}c", "a".repeat(40))
}

/// A sentence long enough that every reading table's scan exceeds the backtracking limit.
fn endless() -> String {
    format!("todo mes{}", " a".repeat(20_000))
}

const EXHAUSTED: PhraseError = PhraseError::Match(MatchError::Engine);

fn continuous(gap: i64) -> Value {
    object_of(vec![("kind", text("continuous")), ("gap", int(gap))])
}

#[test]
fn every_table_compiles_and_the_generated_tables_are_sorted() {
    assert_eq!(COMPILED.negations.len(), 8);
    assert_eq!(COMPILED.readings.iter().map(Vec::len).sum::<usize>(), 42);
    assert_eq!(TZDATA_VERSION, "2026c");
    assert!(ZONE_NAMES.windows(2).all(|pair| pair[0] < pair[1]));
    assert!(DECIMAL_ZEROS.windows(2).all(|pair| pair[0] + 10 <= pair[1]));
    assert_eq!(DIGITS.len(), 31);
}

#[test]
fn digits_of_three_scripts_and_the_fullwidth_colon_become_ascii() {
    assert_eq!(
        translate_digits("\u{663}\u{6f4}\u{ff15}\u{ff1a}x\u{9e9}"),
        "345:x\u{9e9}"
    );
}

#[test]
fn sentences_end_at_their_marks_and_questions_state_nothing() {
    assert_eq!(
        sentences("A!!B.1.c. D?  E\u{3002}F"),
        ["a!", "!", "b.", "1.c.", "  e\u{3002}", "f"]
    );
    assert_eq!(sentences("why? \u{61f}"), Vec::<String>::new());
    assert_eq!(sentences(".x"), vec![".", "x"]);
    assert_eq!(sentences("\u{9e9}.x"), vec!["\u{9e9}.x"]);
}

#[test]
fn intervals_days_and_times_read_in_every_script() {
    assert_eq!(
        stated("every \u{9e9}\u{9e6} seconds", None),
        Ok(vec![continuous(30)])
    );
    assert_eq!(
        stated("a cada \u{967}\u{966} minutos", None),
        Ok(vec![continuous(600)])
    );
    assert_eq!(
        stated("every \u{11068} hours", None),
        Ok(vec![continuous(7200)])
    );
    assert_eq!(
        stated("毎月\u{665}日9時", None),
        Ok(vec![object_of(vec![
            ("kind", text("monthly")),
            ("day", int(5)),
            ("time", text("09:00")),
        ])])
    );
    assert_eq!(
        stated("every monday at 9am and 9pm", None),
        Ok(vec![
            object_of(vec![
                ("kind", text("weekly")),
                ("weekday", int(0)),
                ("time", text("09:00"))
            ]),
            object_of(vec![
                ("kind", text("weekly")),
                ("weekday", int(0)),
                ("time", text("21:00"))
            ]),
        ])
    );
    assert_eq!(stated("every month on the 30th at 9am", None), Ok(vec![]));
    assert_eq!(
        stated("every hour, every hour", None),
        Ok(vec![continuous(3600)])
    );
}

#[test]
fn negations_reject_readings_in_their_language_only_when_one_is_named() {
    assert_eq!(stated("Não quero a cada 30 segundos", None), Ok(vec![]));
    assert_eq!(
        stated("no cloudflare a cada 30 segundos", Some("pt")),
        Ok(vec![continuous(30)])
    );
    assert_eq!(stated("no cloudflare a cada 30 segundos", None), Ok(vec![]));
    assert_eq!(stated("毎日9時はない", None), Ok(vec![]));
    assert_eq!(stated("every hour", Some("")), Ok(vec![continuous(3600)]));
    assert_eq!(stated("every hour", Some("xx")), Err(PhraseError::Language));
    assert_eq!(stated("plain words", Some("xx")), Ok(vec![]));
    assert_eq!(
        Negations::new(&COMPILED, "a").reach("xx", 0),
        Err(PhraseError::Language)
    );
}

#[test]
fn outputs_read_choices_conditions_and_clauses() {
    assert_eq!(
        outputs("show the result only when it changes"),
        Ok(vec!["changes"])
    );
    assert_eq!(
        outputs("show the result, or only when it changes"),
        Ok(vec!["show", "changes"])
    );
    assert_eq!(
        outputs("show it every run whenever it changes"),
        Ok(vec!["show", "changes"])
    );
    assert_eq!(outputs("whenever it changes"), Ok(vec![]));
    assert_eq!(outputs("don't show"), Ok(vec!["none"]));
    assert_eq!(outputs("não mostrar sempre"), Ok(vec!["none"]));
    assert_eq!(outputs("never show me the results"), Ok(vec![]));
}

#[test]
fn routine_nouns_are_read_affirmatively() {
    let found = [
        requests_routine("create a routine"),
        requests_routine("not a routine"),
        requests_routine("a routine?"),
    ];
    assert_eq!(found, [Ok(true), Ok(false), Ok(false)]);
}

#[test]
fn questions_about_what_team_reads_are_found() {
    let found = [
        team_asks("How often should it run?", Some("en")),
        team_asks("every hour?", None),
        team_asks("show the result?", None),
        team_asks("¿Con qué frecuencia?", None),
        team_asks("Which zone?", None),
    ];
    assert_eq!(found, [Ok(true), Ok(true), Ok(true), Ok(true), Ok(false)]);
}

#[test]
fn zones_are_exact_loaded_names() {
    assert_eq!(
        zones("Europe/Lisbon, UTC and Europe/Lisbon or Mars/Base (Asia/Tokyo) UTC+3"),
        Ok(vec![
            "Europe/Lisbon".to_owned(),
            "UTC".to_owned(),
            "Asia/Tokyo".to_owned()
        ])
    );
    assert_eq!(
        [loads("Etc/GMT+3"), loads("utc"), loads("America")],
        [true, false, false]
    );
}

#[test]
fn an_exhausted_search_refuses_the_whole_reading() {
    let long = endless();
    let small = Compiled::new(SMALL_LIMIT);
    assert_eq!(readings(&small, &long, None).map(|_| ()), Err(EXHAUSTED));
    assert_eq!(stated_in(&small, &long, None), Err(EXHAUSTED));
    assert_eq!(outputs_in(&small, &long), Err(EXHAUSTED));
    assert_eq!(requests_routine_in(&small, &long), Err(EXHAUSTED));
    assert_eq!(team_asks_in(&small, &long, None), Err(EXHAUSTED));
    assert_eq!(zones_by(&exhausting(), &exhausting_text()), Err(EXHAUSTED));
}

#[test]
fn a_lazy_reading_finds_what_python_finds() {
    let prefix = Pattern::new(r"\bmonthly\b").expect("pattern");
    let suffix = Pattern::new(r"\b(\d{1,2})th\b").expect("pattern");
    let spans = |text: &str| -> Vec<(usize, usize, Option<String>)> {
        lazy_finditer(&prefix, &suffix, text)
            .expect("completed")
            .iter()
            .map(|found| {
                (
                    found.start,
                    found.end,
                    found.group(text, 1).map(str::to_owned),
                )
            })
            .collect()
    };
    assert_eq!(
        spans("monthly x 5th monthly 6th"),
        [
            (0, 13, Some("5".to_owned())),
            (14, 25, Some("6".to_owned()))
        ]
    );
    assert_eq!(
        spans("monthly monthly 7th"),
        [(0, 19, Some("7".to_owned()))]
    );
    assert_eq!(
        spans("monthly\n5th monthly 6th"),
        [(12, 23, Some("6".to_owned()))]
    );
    assert_eq!(spans("monthly monthly no day"), []);
    assert_eq!(
        lazy_finditer(&exhausting(), &suffix, &exhausting_text()),
        Err(MatchError::Engine)
    );
    assert_eq!(
        lazy_finditer(
            &prefix,
            &exhausting(),
            &format!("monthly {}", exhausting_text())
        ),
        Err(MatchError::Engine)
    );
}

#[test]
fn an_exhausted_negation_or_clause_search_refuses_too() {
    let text = exhausting_text();
    assert_eq!(negation_starts(&exhausting(), &text), Err(EXHAUSTED));
    assert_eq!(negation_starts(&exhausting(), "ab"), Ok(vec![0]));
    let end = text.len();
    assert_eq!(
        one_clause(&exhausting(), &text, (0, 0), (end, end)),
        Err(EXHAUSTED)
    );
    assert_eq!(one_clause(&exhausting(), &text, (0, end), (0, 0)), Ok(true));
}

#[test]
fn readings_keep_what_a_later_negation_does_not_reach() {
    assert_eq!(
        stated("every hour não a cada 30 segundos", None),
        Ok(vec![continuous(3600)])
    );
    for text in [
        "every hour",
        "todo dia às 9h",
        "every monday at 9am",
        "毎月5日9時",
    ] {
        let found = stated(text, None).expect("a reading");
        assert!(found.iter().all(crate::routine::exact_schedule), "{text}");
    }
    assert_eq!(
        outputs("whenever it changes. show it every run. show the result"),
        Ok(vec!["show", "changes"])
    );
    assert_eq!(
        outputs("show it every run. whenever it changes. only when it changes"),
        Ok(vec!["show", "changes"])
    );
}

#[test]
fn a_long_run_of_spaces_before_a_missing_unit_reads_like_python() {
    let spaces = " ".repeat(3000);
    assert_eq!(
        stated(&format!("\" 每{spaces}每{spaces}"), None),
        Ok(vec![])
    );
    assert_eq!(
        stated(&format!("每{spaces}5{spaces}秒"), None),
        Ok(vec![continuous(5)])
    );
    assert_eq!(
        stated(&format!("كل{spaces}ساعة"), None),
        Ok(vec![continuous(3600)])
    );
    assert_eq!(stated(&format!("كل {spaces}"), None), Ok(vec![]));
}

#[test]
fn a_negation_reaches_only_a_reading_after_it_and_spans_overlap_only_when_they_share_text() {
    let mut negations = Negations::new(&COMPILED, "não cada dia");
    assert_eq!(negations.reach("pt", 0), Ok(false));
    assert_eq!(negations.reach("pt", 4), Ok(true));
    let reading = Match {
        start: 2,
        end: 4,
        groups: Vec::new(),
    };
    assert!(!overlaps(&reading, &[(0, 2), (4, 6)]));
    assert!(overlaps(&reading, &[(3, 5)]));
}

#[test]
fn a_weekday_reads_its_own_number() {
    // Python: stated("toda terça às 9h") is one weekly schedule on weekday 1.
    let weekly = object_of(vec![
        ("kind", text("weekly")),
        ("weekday", int(1)),
        ("time", text("09:00")),
    ]);
    assert_eq!(stated("toda terça às 9h", None), Ok(vec![weekly]));
}
