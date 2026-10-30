//! `test_routine_dates.py`: dates inside a recorded text, which each run writes again for its own date.

use std::collections::BTreeSet;

use strict_json::Value;

use super::{MAX_PARTS, admitted, dates, render, shown, template};
use crate::date::Date;
use crate::testing::json;

fn day(year: i32, month: i32, day: i32) -> Date {
    Date::new(year, month, day).expect("a date")
}

fn recorded_on() -> Date {
    day(2026, 10, 15)
}

fn tomorrow() -> Date {
    day(2026, 10, 16)
}

fn templated(text: &str, locale: Option<&str>, named: &[Date]) -> Option<Vec<Value>> {
    template(text, recorded_on(), locale, |date| named.contains(&date))
}

fn format_of(parts: &[Value], index: usize) -> &str {
    crate::json::as_str(crate::json::at(&parts[index], "format")).expect("a format")
}

#[test]
fn each_written_form_moves_with_the_run_date() {
    let cases = [
        (
            "notícias IA hoje 15 outubro 2026",
            "pt",
            "notícias IA hoje 16 outubro 2026",
        ),
        (
            "notícias de 15 de outubro de 2026",
            "pt",
            "notícias de 16 de outubro de 2026",
        ),
        (
            "jogos 15/10/2026 a 21/10/2026",
            "pt",
            "jogos 16/10/2026 a 22/10/2026",
        ),
        ("report 2026-10-15", "pt", "report 2026-10-16"),
        ("weather 5/10", "pt", "weather 6/10"),
        ("AI news October 15, 2026", "en", "AI news October 16, 2026"),
        ("news 10/14/2026", "en", "news 10/15/2026"),
        (
            "Nachrichten 15. Oktober 2026",
            "de",
            "Nachrichten 16. Oktober 2026",
        ),
        (
            "actualités 15 octobre 2026",
            "fr",
            "actualités 16 octobre 2026",
        ),
        ("ニュース 2026年10月15日", "ja", "ニュース 2026年10月16日"),
        ("أخبار 15 أكتوبر 2026", "ar", "أخبار 16 أكتوبر 2026"),
        ("NOTÍCIAS 15 OUTUBRO", "pt", "NOTÍCIAS 16 OUTUBRO"),
        ("news 31/10/2026", "pt", "news 01/11/2026"),
    ];
    for (text, locale, next) in cases {
        let parts =
            templated(text, Some(locale), &[]).unwrap_or_else(|| panic!("a template of {text}"));
        assert_eq!(render(&parts, recorded_on()).as_deref(), Some(text));
        assert_eq!(render(&parts, tomorrow()).as_deref(), Some(next));
        assert!(admitted(&Value::Array(parts)));
    }
}

#[test]
fn text_without_a_movable_date_stays_a_literal() {
    for text in [
        "notícias de inteligência artificial",
        "notícias outubro 2026",
        "relatório 2026",
        "15 de abril de 2026",
        "1/2 xícara",
        "jogos de 15 a 21/10/2026",
        "15/10/2026 a 3 dias",
        "oUtubro 15",
    ] {
        assert_eq!(templated(text, Some("pt"), &[]), None, "{text}");
    }
}

#[test]
fn a_reading_two_languages_or_orders_share_is_the_interface_languages_or_none() {
    // "10/10/2026" reads day first or month first; "agosto" is Portuguese and Spanish.
    let pt = templated("news 10/10/2026", Some("pt"), &[]).expect("a template");
    assert_eq!(format_of(&pt, 1), "{dd}/{MM}/{yyyy}");
    let en = templated("news 10/10/2026", Some("en"), &[]).expect("a template");
    assert_eq!(format_of(&en, 1), "{MM}/{dd}/{yyyy}");
    assert_eq!(templated("news 10/10/2026", None, &[]), None);
    let near_august = day(2026, 8, 20);
    let spanish = template("15 de agosto", near_august, Some("es"), |_| false).expect("a template");
    assert_eq!(crate::json::at(&spanish[0], "lang"), &json(r#""es""#));
    assert_eq!(
        template("15 de agosto", near_august, Some("fr"), |_| false),
        None
    );
}

#[test]
fn the_longest_overlapping_date_wins_and_the_shorter_stays_text() {
    let parts = templated("top 10 October 15, 2026 news", Some("en"), &[]).expect("a template");
    assert_eq!(
        render(&parts, tomorrow()).as_deref(),
        Some("top 10 October 16, 2026 news")
    );
    // A longer date out of reach still covers its text: the shorter date inside it never moves.
    assert_eq!(
        templated("top 10 October 15, 2025 news", Some("en"), &[]),
        None
    );
}

#[test]
fn a_date_the_person_wrote_stays_as_written() {
    let named = [day(2026, 10, 20)];
    let parts = templated("jogos 15/10/2026 e 20/10/2026", Some("pt"), &named).expect("a template");
    assert_eq!(
        render(&parts, tomorrow()).as_deref(),
        Some("jogos 16/10/2026 e 20/10/2026")
    );
    assert_eq!(templated("jogo 20/10/2026", Some("pt"), &named), None);
    assert_eq!(
        dates("o jogo de 20/10", recorded_on()),
        Ok(BTreeSet::from([day(2026, 10, 20)]))
    );
    // The person's words keep every date they may name, beside a range or in either order.
    let wide = dates(
        "IA de 15/10/2026 e de 1 a 5/11/2026, ou 10/11",
        recorded_on(),
    );
    let expected = BTreeSet::from([
        day(2026, 10, 15),
        day(2026, 11, 5),
        day(2026, 10, 11),
        day(2026, 11, 10),
    ]);
    assert_eq!(wide, Ok(expected));
}

#[test]
fn a_template_is_closed() {
    let dated = r#"{"days": 0, "format": "{d}", "lang": null}"#;
    let too_many = format!("[{}]", vec![dated; MAX_PARTS + 1].join(","));
    let long_format = format!(
        r#"[{{"days": 0, "format": "{}", "lang": null}}]"#,
        "{d}".repeat(22)
    );
    for parts in [
        "[]".to_owned(),
        r#"["text"]"#.to_owned(),
        format!(r#"["a", "b", {dated}]"#),
        format!(r#"["", {dated}]"#),
        r#"[{"days": 32, "format": "{d}", "lang": null}]"#.to_owned(),
        r#"[{"days": true, "format": "{d}", "lang": null}]"#.to_owned(),
        r#"[{"days": 0, "format": "{x}", "lang": null}]"#.to_owned(),
        r#"[{"days": 0, "format": "plain", "lang": null}]"#.to_owned(),
        r#"[{"days": 0, "format": "{mmmm}", "lang": null}]"#.to_owned(),
        r#"[{"days": 0, "format": "{d}", "lang": "pt"}]"#.to_owned(),
        long_format,
        r#"[{"days": 0, "format": "{d}"}]"#.to_owned(),
        r#""{d}""#.to_owned(),
        too_many,
    ] {
        assert!(!admitted(&json(&parts)), "{parts}");
    }
}

#[test]
fn a_person_reads_each_date_by_its_format_language_and_offset() {
    let parts = json(
        r#"["{x} ", {"days": 0, "format": "{d} de {mmmm}", "lang": "pt"}, " a ",
            {"days": 6, "format": "{dd}/{MM}", "lang": null}]"#,
    );
    assert_eq!(
        shown(crate::json::elements(&parts)),
        "{{x}} {D de month|pt} a {DD/MM|+6}"
    );
}
