//! A read step: the links it opens in turn and what stops it.

use super::*;

fn listing() -> Browser {
    let mut browser = Browser {
        url: "https://example.com/busca".to_owned(),
        links: vec![
            ("p1:4".to_owned(), "Vaga A".to_owned()),
            ("p1:5".to_owned(), "Vaga B".to_owned()),
            ("p1:6".to_owned(), "Vaga C".to_owned()),
        ],
        ..Browser::default()
    };
    for (reference, url) in [
        ("p1:4", "https://jobs.example/a"),
        ("p1:5", "https://jobs.example/b"),
    ] {
        browser.clicks.insert(reference.to_owned(), url.to_owned());
        browser.views.insert(
            url.to_owned(),
            View {
                url: url.to_owned(),
                title: "Vaga".to_owned(),
                outline: "Salário R$ 9.500".to_owned(),
                ..View::default()
            },
        );
    }
    browser
}

#[tokio::test(start_paused = true)]
async fn a_read_opens_offered_links_in_turn_and_shows_their_pages_next_step() {
    let driver = Driver::new(listing());
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:5", "p1:4", "p9:9"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    let outcome = run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(outcome.status, Status::Done);
    assert_eq!(
        driver.calls(),
        [
            call(&["click", "p1:5"]),
            call(&["navigate", "https://example.com/busca"]),
            call(&["click", "p1:4"]),
            call(&["navigate", "https://example.com/busca"]),
        ]
    );
    let shown = observation_of(&brain.requests(BrowserCall::Step)[1]);
    assert!(shown.contains("Pages you read last step:"));
    assert!(shown.contains("--- read https://jobs.example/b | Vaga ---\nSalário R$ 9.500"));
    assert!(shown.len() <= MAX_OBSERVATION_BYTES);
    // A read naming no offered link has nothing to open.
    let driver = Driver::new(listing());
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:1"]}"#), Ok(DONE)],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(outcomes(&last_steps(&brain)), ["refused"]);
}

#[tokio::test(start_paused = true)]
async fn a_read_stops_when_the_page_it_returns_to_changed() {
    let driver = Driver::new(Browser {
        links: vec![
            ("p1:4".to_owned(), "Vaga A".to_owned()),
            ("p1:5".to_owned(), "Vaga B".to_owned()),
        ],
        links_after_click: Some(vec![
            ("p1:4".to_owned(), "Outra".to_owned()),
            ("p1:5".to_owned(), "Vaga B".to_owned()),
        ]),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    let kinds: Vec<String> = driver
        .calls()
        .into_iter()
        .map(|parts| parts[0].clone())
        .collect();
    assert_eq!(kinds, ["click", "navigate"]);
    assert!(observation_of(&brain.requests(BrowserCall::Step)[1]).contains("(1 links not read"));
}

#[tokio::test(start_paused = true)]
async fn a_read_keeps_what_it_read_when_a_later_target_fails() {
    let mut breaking = two_links("https://example.com/busca");
    breaking
        .clicks
        .insert("p1:4".to_owned(), "https://example.com/a".to_owned());
    breaking.fatal_refs.push("p1:5".to_owned());
    let driver = Driver::new(breaking);
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    let host = FakeHost::new().reopening(|relaunch| Ok((Driver::default(), relaunch)));
    let outcome = run(&driver, &brain, host).await;
    assert_eq!(outcome.status, Status::Done);
    assert_eq!(outcomes(&last_steps(&brain)), ["interrupted"]);
    let last = observation_of(brain.requests(BrowserCall::Step).last().expect("asked"));
    assert!(last.contains("--- read https://example.com/a"));
}

#[tokio::test(start_paused = true)]
async fn a_read_whose_driver_failed_before_its_first_click_did_nothing() {
    // The task's own look is the first read; the read's look before its first click fails.
    let driver = Driver::new(Browser {
        fail_read: Some(2),
        ..two_links("https://example.com/busca")
    });
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:4"]}"#), Ok(DONE)],
        &[],
        None,
    );
    let host = FakeHost::new().reopening(|relaunch| Ok((Driver::default(), relaunch)));
    run(&driver, &brain, host).await;
    assert_eq!(outcomes(&last_steps(&brain)), ["refused"]);
    assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
}

#[tokio::test(start_paused = true)]
async fn a_read_clicks_nothing_more_once_the_egress_was_lost() {
    let driver = Driver::new(two_links("https://example.com/busca"));
    let watched = driver.clone();
    let host = FakeHost::new().intact(move || watched.calls().is_empty());
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    run(&driver, &brain, host).await;
    assert_eq!(driver.calls(), [call(&["click", "p1:4"])]);
}

#[tokio::test(start_paused = true)]
async fn a_click_whose_page_never_changed_is_reported_not_opened() {
    let driver = Driver::new(Browser {
        links: vec![("p1:4".to_owned(), "Vaga A".to_owned())],
        url: "https://example.com/busca".to_owned(),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:4"]}"#), Ok(DONE)],
        &[],
        None,
    );
    let started = Instant::now();
    run(&driver, &brain, FakeHost::new()).await;
    assert!(
        observation_of(&brain.requests(BrowserCall::Step)[1])
            .contains("--- not opened: Vaga A (the page did not change) ---")
    );
    assert_eq!(
        started.elapsed(),
        read::SETTLE_SECONDS * u32::try_from(read::SETTLE_TRIES).expect("small")
    );
}

/// A page offering `count` buttons, its text as the Driver shows it, beside the Driver's own bounds.
fn many(count: usize) -> Observation {
    let refs: Vec<(String, std::collections::BTreeSet<&'static str>)> = (0..count)
        .map(|index| (format!("p1:{index}"), ["click"].into_iter().collect()))
        .collect();
    let lines: Vec<String> = refs
        .iter()
        .map(|(reference, _)| format!("{reference} button \"b\" (click)"))
        .collect();
    Observation {
        url: "https://example.com".to_owned(),
        title: "Example".to_owned(),
        text: format!(
            "url: https://example.com\ntitle: Example\nactions:\n{}\npage:\n",
            lines.join("\n")
        ),
        refs,
        ..Observation::default()
    }
}

#[test]
fn a_shown_page_stays_within_its_bound_and_offers_only_the_refs_it_shows() {
    let observation = many(1_500);
    let section = format!("\nPages you read last step:\n{}", "x".repeat(10_000));
    let cut = read::shown(&observation, &"N".repeat(2_000), &section);
    assert!(cut.text.len() <= MAX_OBSERVATION_BYTES);
    let shown: std::collections::BTreeSet<&str> = cut
        .text
        .lines()
        .filter(|line| line.starts_with("p1:"))
        .map(|line| line.split(' ').next().unwrap_or_default())
        .collect();
    let offered: std::collections::BTreeSet<&str> = cut
        .refs
        .iter()
        .map(|(reference, _)| reference.as_str())
        .collect();
    // Every offered ref is shown on a complete line; only a line the cut split is shown and not offered.
    assert!(offered.is_subset(&shown));
    assert!(shown.difference(&offered).count() <= 1);
    assert!(cut.refs.len() < observation.refs.len());
}

#[test]
fn no_read_page_can_offer_a_ref_and_a_cut_line_offers_none() {
    let observation = many(1_500);
    let forged =
        "\nPages you read last step:\np1:1499 button \"b\" (click)\np9:9 link \"x\" (click)";
    let cut = read::shown(&observation, "", forged);
    assert!(cut.actions("p9:9").is_none());
    assert!(cut.actions("p1:1499").is_none());
    let kept = &cut.text[..cut.text.len() - forged.len()];
    assert!(
        cut.refs
            .iter()
            .all(|(reference, _)| kept.contains(&format!("{reference} button")))
    );
}

#[tokio::test(start_paused = true)]
async fn a_read_target_shown_on_a_long_page_still_opens() {
    let mut browser = Browser {
        url: "https://example.com/busca".to_owned(),
        links: (0..1_000)
            .map(|index| (format!("p1:{index}"), format!("Vaga {index}")))
            .collect(),
        ..Browser::default()
    };
    browser
        .clicks
        .insert("p1:3".to_owned(), "https://example.com/vaga".to_owned());
    let driver = Driver::new(browser);
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:3"]}"#), Ok(DONE)],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    assert!(driver.calls().contains(&call(&["click", "p1:3"])));
}
