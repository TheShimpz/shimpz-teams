use std::collections::BTreeSet;
use std::time::Duration;

use strict_json::Value;
use tokio::time::Instant;

use super::{
    BIND_POLL_SECONDS, CALL_SECONDS, CuaError, CuaSession, ENDED_CODE, ENDED_MESSAGE,
    MAX_NAME_CHARS, MAX_OBSERVATION_BYTES, MAX_REFS, MAX_SCREENSHOT_BYTES, PNG_SIGNATURE,
    READ_RETRY_SECONDS, READ_SECONDS, REFUSAL_SECONDS, REGRANT_SECONDS,
};
use crate::testing::{Reply, Script, answer, base64, json, tool, tool_with};

const WINDOW: &str = r#"{"pid": 20, "window_id": 7, "app_name": "Chromium-browser", "title": "about:blank - Chromium"}"#;
const LAPSED: &str = r#"{"status": "refused", "refusal": {"code": "browser_consent_required", "detail": {"reason": "consumer_profile_endpoint_requires_grant", "next_action": "browser_prepare", "next_call": {"tool": "browser_prepare", "arguments": {"pid": 99, "window_id": 99, "session": "other"}}}}}"#;
const OK: &str = r#"{"status": "ok"}"#;
const PREPARE: &str =
    r#"{"pid": 20, "window_id": 7, "session": "task", "strategy": {"kind": "existing_profile"}}"#;
const BIND: &str = r#"{"pid": 20, "window_id": 7, "session": "task"}"#;

fn bound_with(tabs: &str, quality: &str) -> String {
    format!(r#"{{"binding_quality": "{quality}", "target_id": "bt-1", "tabs": {tabs}}}"#)
}

fn bound() -> Reply {
    tool(&bound_with(
        r#"[{"tab_id": "tab-1", "active": true}]"#,
        "exact",
    ))
}

fn windows(list: &str) -> Reply {
    tool(&format!(r#"{{"windows": {list}}}"#))
}

fn opening() -> Vec<Reply> {
    vec![
        answer(r#"{"result": {"capabilities": {}}}"#),
        windows(&format!("[{WINDOW}]")),
        tool(OK),
        bound(),
    ]
}

async fn opened(replies: impl IntoIterator<Item = Reply>) -> (CuaSession, Script) {
    let script = Script::new(opening());
    script.extend(replies);
    let mut session = CuaSession::new(script.channel());
    session.open().await.expect("the session opens");
    (session, script)
}

fn set(actions: &[&'static str]) -> BTreeSet<&'static str> {
    actions.iter().copied().collect()
}

fn failure(error: CuaError) -> (&'static str, bool) {
    (error.code, error.fatal)
}

#[tokio::test(start_paused = true)]
async fn counting_windows_binds_nothing_and_skips_the_drivers_own_overlay() {
    let overlay = r#"{"pid": 21, "window_id": 9, "app_name": ""}"#;
    let script = Script::new([
        answer(r#"{"result": {"capabilities": {}}}"#),
        windows(&format!("[{WINDOW}, {WINDOW}, {overlay}]")),
    ]);
    let mut session = CuaSession::new(script.channel());
    session.initialize().await.expect("initialized");
    assert_eq!(session.chromium_windows().await, Ok(2));
    assert_eq!(script.calls(), [("list_windows".to_owned(), json("{}"))]);
    for listed in [r#""none""#, &format!(r#"[{WINDOW}, "window"]"#)] {
        let script = Script::new([
            answer(r#"{"result": {"capabilities": {}}}"#),
            windows(listed),
        ]);
        let mut session = CuaSession::new(script.channel());
        session.initialize().await.expect("initialized");
        assert!(session.chromium_windows().await.is_err());
    }
}

#[tokio::test(start_paused = true)]
async fn opening_binds_the_one_chromium_window_exactly() {
    let (session, script) = opened([]).await;
    let sent = script.sent();
    assert_eq!(
        sent[0].get("method"),
        Some(&Value::String("initialize".to_owned()))
    );
    assert_eq!(
        sent[0].get("params"),
        Some(&json(
            r#"{"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "shimpz-team", "version": "1"}}"#
        ))
    );
    assert_eq!(
        sent[1],
        json(r#"{"jsonrpc": "2.0", "method": "notifications/initialized"}"#)
    );
    assert_eq!(
        script.calls(),
        [
            ("list_windows".to_owned(), json("{}")),
            ("browser_prepare".to_owned(), json(PREPARE)),
            ("get_browser_state".to_owned(), json(BIND)),
        ]
    );
    assert_eq!(
        session.tab(),
        Ok(json(
            r#"{"session": "task", "target_id": "bt-1", "tab_id": "tab-1"}"#
        ))
    );
}

#[tokio::test(start_paused = true)]
async fn requests_are_compact_ascii_json_rpc_lines_numbered_from_one() {
    let (mut session, script) = opened([tool(OK)]).await;
    session
        .navigate("https://example.com/ação")
        .await
        .expect("navigated");
    let sent = script.sent();
    assert_eq!(sent[0].get("id"), Some(&json("1")));
    assert_eq!(
        sent.last().and_then(|message| message.get("id")),
        Some(&json("5"))
    );
    // The Channel sees exactly the encoded line: compact, ASCII-escaped, newline-terminated.
    let lines = script.lines();
    assert_eq!(
        lines.last().map(String::as_str),
        Some(concat!(
            r#"{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"browser_navigate","#,
            r#""arguments":{"session":"task","target_id":"bt-1","tab_id":"tab-1","#,
            r#""url":"https://example.com/a\u00e7\u00e3o"}}}"#,
            "\n"
        ))
    );
    assert_eq!(
        lines[1],
        "{\"jsonrpc\": \"2.0\", \"method\": \"notifications/initialized\"}\n"
    );
}

#[tokio::test(start_paused = true)]
async fn opening_refuses_anything_but_one_exactly_bound_chromium_tab() {
    let one = format!("[{WINDOW}]");
    let window_cases = [
        "[]".to_owned(),
        format!("[{WINDOW}, {WINDOW}]"),
        r#"["window"]"#.to_owned(),
        "null".to_owned(),
        format!("[{}]", WINDOW.replace("Chromium-browser", "Xterm")),
        format!("[{}]", WINDOW.replace(r#""pid": 20"#, r#""pid": "20""#)),
        format!(
            "[{}]",
            WINDOW.replace(r#""window_id": 7"#, r#""window_id": true"#)
        ),
    ];
    let mut cases: Vec<(&str, Vec<Reply>)> = window_cases
        .iter()
        .map(|list| ("browser-window-unavailable", vec![windows(list)]))
        .collect();
    cases.push((
        "browser-attach-refused",
        vec![windows(&one), tool(r#"{"status": "refused"}"#)],
    ));
    for inexact in [
        bound_with(r#"[{"tab_id": "tab-1", "active": true}]"#, "heuristic"),
        r#"{"binding_quality": "exact", "target_id": 3, "tabs": [{"tab_id": "tab-1", "active": true}]}"#.to_owned(),
        r#"{"binding_quality": "exact", "target_id": "bt-1", "tabs": "tab"}"#.to_owned(),
        bound_with(
            r#"[{"tab_id": "a", "active": true}, {"tab_id": "b", "active": true}]"#,
            "exact",
        ),
        bound_with(r#"["tab", {"tab_id": 3, "active": true}]"#, "exact"),
    ] {
        cases.push((
            "browser-binding-inexact",
            vec![windows(&one), tool(OK), tool(&inexact)],
        ));
    }
    for (code, script) in cases {
        // Each fails at its first answer: none is a tab still waiting for its active mark.
        let script = Script::new(std::iter::once(answer(r#"{"result": {}}"#)).chain(script));
        let mut session = CuaSession::new(script.channel());
        let error = session.open().await.expect_err("refused");
        assert_eq!(failure(error), (code, true));
    }
}

#[tokio::test(start_paused = true)]
async fn a_tab_mid_navigation_is_waited_for_until_one_is_active() {
    let unmarked = || {
        tool(&bound_with(
            r#"[{"tab_id": "tab-2", "active": null}]"#,
            "exact",
        ))
    };
    let (mut session, script) = opened([
        unmarked(),
        unmarked(),
        tool(&bound_with(
            r#"[{"tab_id": "tab-2", "active": true}]"#,
            "exact",
        )),
    ])
    .await;
    let started = Instant::now();
    session.bind(None).await.expect("bound");
    assert_eq!(
        session.tab().expect("bound").get("tab_id"),
        Some(&json(r#""tab-2""#))
    );
    assert_eq!(started.elapsed(), BIND_POLL_SECONDS * 2);
    // One that never marks a tab active hung: it is lost once a call's whole bound is spent, or the task's.
    let polls = (CALL_SECONDS.as_millis() / BIND_POLL_SECONDS.as_millis()) as usize + 1;
    script.extend((0..polls).map(|_| unmarked()));
    let started = Instant::now();
    let error = session.bind(None).await.expect_err("hung");
    assert_eq!(error, CuaError::fatal("browser-binding-inexact").into());
    assert!(started.elapsed() <= CALL_SECONDS);
    assert!(started.elapsed() + Duration::from_secs(1) > CALL_SECONDS);
    session.set_deadline(Some(Instant::now() + Duration::from_secs(1)));
    script.extend((0..polls).map(|_| unmarked()));
    let started = Instant::now();
    assert!(session.bind(None).await.is_err());
    assert!(started.elapsed() <= Duration::from_secs(1));
}

#[tokio::test(start_paused = true)]
async fn slow_answers_never_stretch_binding_past_its_one_bound() {
    let unmarked = || {
        tool(&bound_with(
            r#"[{"tab_id": "tab-2", "active": null}]"#,
            "exact",
        ))
    };
    let (mut session, script) = opened((0..10).map(|_| unmarked())).await;
    script.delay_receives(Duration::from_secs(10));
    let started = Instant::now();
    let before = script.deadlines().len();
    assert!(session.bind(None).await.is_err());
    let deadlines = script.deadlines()[before..].to_vec();
    assert_ne!(deadlines.len(), 0);
    assert!(
        deadlines
            .iter()
            .all(|deadline| *deadline <= started + CALL_SECONDS)
    );
    assert!(started.elapsed() <= CALL_SECONDS + Duration::from_secs(10));
}

#[tokio::test(start_paused = true)]
async fn a_malformed_active_tab_never_hides_a_second_one() {
    let (mut session, _script) = opened([tool(&bound_with(
        r#"[{"tab_id": "a", "active": true}, {"tab_id": 3, "active": true}]"#,
        "exact",
    ))])
    .await;
    assert_eq!(
        session.bind(None).await,
        Err(CuaError::fatal("browser-binding-inexact").into())
    );
}

#[tokio::test(start_paused = true)]
async fn nothing_is_called_before_the_session_is_open() {
    let script = Script::default();
    let mut session = CuaSession::new(script.channel());
    assert_eq!(session.tab(), Err(CuaError::fatal("browser-not-open")));
    assert_eq!(
        session.bind(None).await,
        Err(CuaError::fatal("browser-not-open").into())
    );
    assert_eq!(
        session.navigate("https://example.com").await,
        Err(CuaError::fatal("browser-not-open"))
    );
    assert_eq!(
        session.call("run_script", json("{}"), false, None).await,
        Err(CuaError::fatal("tool-not-admitted").into())
    );
    assert_eq!(script.sent().len(), 0);
}

fn sparse_page() -> String {
    let outline = format!(
        "- heading \"Editais\"\n{}",
        "x".repeat(MAX_OBSERVATION_BYTES * 2)
    );
    let long = "B".repeat(500);
    format!(
        r#"{{"page": {{"url": "https://example.com/\u0007path", "title": "Example\nDomain"}}, "refs": [
            {{"ref": "p1:0", "role": "textbox", "name": "Buscar\u0000", "actions": ["type", "pointer"]}},
            {{"ref": "p1:1", "role": "button", "name": "{long}", "actions": ["click"]}},
            {{"ref": "p1:2", "role": "generic", "name": "x", "actions": ["dom_only"]}},
            {{"ref": 3, "actions": ["click"]}},
            {{"ref": "p1:4", "actions": "click"}},
            {{"ref": "button\n", "actions": ["click"]}},
            "junk"
        ], "outline": "{}"}}"#,
        outline.replace('\n', "\\n").replace('"', "\\\"")
    )
}

#[tokio::test(start_paused = true)]
async fn an_observation_is_bounded_text_with_the_refs_each_action_may_target() {
    let (mut session, script) = opened([bound(), tool(&sparse_page())]).await;
    let observation = session.observe(false).await.expect("observed");
    assert_eq!(
        (observation.url.as_str(), observation.title.as_str()),
        ("https://example.com/ path", "Example Domain")
    );
    assert_eq!(
        observation.refs,
        [
            ("p1:0".to_owned(), set(&["type", "pointer"])),
            ("p1:1".to_owned(), set(&["click"])),
        ]
    );
    assert!(
        observation
            .text
            .starts_with("url: https://example.com/ path\ntitle: Example Domain\nactions:\n")
    );
    assert!(
        observation
            .text
            .contains("p1:0 textbox \"Buscar\" (pointer, type)")
    );
    assert!(observation.text.contains(&format!(
        "p1:1 button \"{}\" (click)",
        "B".repeat(MAX_NAME_CHARS)
    )));
    assert!(
        observation
            .text
            .contains("\npage:\n- heading \"Editais\"\nxxx")
    );
    assert_eq!(observation.text.len(), MAX_OBSERVATION_BYTES);
    assert_eq!(observation.screenshot, None);
    assert_eq!(
        script.calls().last().cloned(),
        Some((
            "get_browser_state".to_owned(),
            json(
                r#"{"session": "task", "target_id": "bt-1", "tab_id": "tab-1", "snapshot_format": "semantic_v2", "include_screenshot": false}"#
            )
        ))
    );
}

#[tokio::test(start_paused = true)]
async fn an_observation_follows_the_active_tab_and_tolerates_a_sparse_snapshot() {
    let moved = bound_with(
        r#"[{"tab_id": "tab-1", "active": false}, {"tab_id": "tab-2", "active": true}]"#,
        "exact",
    );
    let (mut session, script) = opened([
        tool(&moved),
        tool(r#"{"page": "none", "refs": null, "outline": 3}"#),
    ])
    .await;
    let observation = session.observe(false).await.expect("observed");
    assert_eq!(
        (
            observation.url.as_str(),
            observation.title.as_str(),
            observation.refs.len()
        ),
        ("", "", 0)
    );
    assert_eq!(observation.text, "url: \ntitle: \nactions:\n\npage:\n");
    assert_eq!(
        script
            .calls()
            .last()
            .and_then(|(_, arguments)| arguments.get("tab_id").cloned()),
        Some(json(r#""tab-2""#))
    );
}

#[tokio::test(start_paused = true)]
async fn an_observation_keeps_at_most_its_bound_of_refs() {
    let refs: Vec<String> = (0..MAX_REFS + 5)
        .map(|index| {
            format!(r#"{{"ref": "p1:{index}", "role": "link", "name": "n", "actions": ["click"]}}"#)
        })
        .collect();
    let (mut session, _script) = opened([
        bound(),
        tool(&format!(r#"{{"refs": [{}]}}"#, refs.join(","))),
    ])
    .await;
    let observation = session.observe(false).await.expect("observed");
    assert_eq!(observation.refs.len(), MAX_REFS);
    assert_eq!(observation.links.len(), MAX_REFS);
}

#[tokio::test(start_paused = true)]
async fn an_observation_offers_only_the_refs_it_shows_within_its_byte_bound() {
    let name = "ç".repeat(MAX_NAME_CHARS);
    let refs: Vec<String> = (0..MAX_REFS)
        .map(|index| {
            format!(
                r#"{{"ref": "p1:{index}", "role": "link", "name": "{name}", "actions": ["click"]}}"#
            )
        })
        .collect();
    let state = format!(
        r#"{{"page": {{"url": "{}"}}, "refs": [{}]}}"#,
        "é".repeat(2048),
        refs.join(",")
    );
    let (mut session, _script) = opened([bound(), tool(&state)]).await;
    let observation = session.observe(false).await.expect("observed");
    assert!(observation.text.len() <= MAX_OBSERVATION_BYTES);
    assert!(observation.refs.len() < MAX_REFS);
    let shown: Vec<String> = observation
        .text
        .split("\nactions:\n")
        .nth(1)
        .and_then(|rest| rest.split("\npage:\n").next())
        .expect("actions")
        .lines()
        .map(|line| line.split(' ').next().unwrap_or_default().to_owned())
        .collect();
    let offered: Vec<String> = observation
        .refs
        .iter()
        .map(|(reference, _)| reference.clone())
        .collect();
    assert_eq!(shown, offered);
}

fn screenshot_cases() -> Vec<(String, Option<Vec<u8>>)> {
    let png = [PNG_SIGNATURE, b"pixels"].concat();
    let encoded = base64(&png);
    let oversized = base64(&[png.as_slice(), &vec![b'x'; MAX_SCREENSHOT_BYTES]].concat());
    let image = |media: &str, data: &str| {
        format!(r#"{{"type": "image", "mimeType": "{media}", "data": {data}}}"#)
    };
    let quoted = |text: &str| format!("\"{text}\"");
    vec![
        (
            format!(
                r#"[{{"type": "text", "text": "x"}}, {}]"#,
                image("image/png", &quoted(&encoded))
            ),
            Some(png.clone()),
        ),
        (
            format!("[{}]", image("image/jpeg", &quoted(&encoded))),
            None,
        ),
        (
            format!("[{}]", image("image/png", &quoted(&base64(b"GIF89a")))),
            None,
        ),
        (
            format!("[{}]", image("image/png", &quoted("not base64!"))),
            None,
        ),
        (
            format!(
                "[{}]",
                image("image/png", &quoted(&encoded[..encoded.len() - 1]))
            ),
            None,
        ),
        (format!("[{}]", image("image/png", "7")), None),
        (
            format!(
                "[{}]",
                image("image/png", &quoted(&"A".repeat(MAX_SCREENSHOT_BYTES * 2)))
            ),
            None,
        ),
        (
            format!("[{}]", image("image/png", &quoted(&oversized))),
            None,
        ),
        (r#""content""#.to_owned(), None),
    ]
}

#[tokio::test(start_paused = true)]
async fn a_requested_screenshot_is_one_bounded_png() {
    for (content, expected) in screenshot_cases() {
        let snapshot = answer(&format!(
            r#"{{"result": {{"structuredContent": {{"page": {{}}}}, "content": {content}}}}}"#
        ));
        let (mut session, script) = opened([bound(), snapshot]).await;
        let observation = session.observe(true).await.expect("observed");
        assert_eq!(observation.screenshot, expected);
        assert_eq!(
            script
                .calls()
                .last()
                .and_then(|(_, arguments)| arguments.get("include_screenshot").cloned()),
            Some(Value::Bool(true))
        );
    }
}

#[tokio::test(start_paused = true)]
async fn actions_send_exactly_their_closed_arguments() {
    let (mut session, script) = opened((0..5).map(|_| tool(OK))).await;
    session
        .navigate("https://example.com/a")
        .await
        .expect("navigated");
    session.click("p1:1").await.expect("clicked");
    session
        .type_text("p1:0", "inteligência artificial")
        .await
        .expect("typed");
    session.scroll("down").await.expect("scrolled");
    session.scroll("up").await.expect("scrolled");
    let tab = r#""session": "task", "target_id": "bt-1", "tab_id": "tab-1""#;
    let calls = script.calls();
    assert_eq!(
        calls[calls.len() - 5..],
        [
            (
                "browser_navigate".to_owned(),
                json(&format!(r#"{{{tab}, "url": "https://example.com/a"}}"#))
            ),
            (
                "browser_click".to_owned(),
                json(&format!(
                    r#"{{{tab}, "ref": "p1:1", "delivery_mode": "foreground"}}"#
                ))
            ),
            (
                "browser_type".to_owned(),
                json(&format!(
                    r#"{{{tab}, "ref": "p1:0", "text": "inteligência artificial", "mode": "insert_text", "replace": true}}"#
                ))
            ),
            (
                "browser_pointer".to_owned(),
                json(&format!(
                    r#"{{{tab}, "action": "scroll", "delta_y": 600, "x": 640, "y": 400, "delivery_mode": "foreground"}}"#
                ))
            ),
            (
                "browser_pointer".to_owned(),
                json(&format!(
                    r#"{{{tab}, "action": "scroll", "delta_y": -600, "x": 640, "y": 400, "delivery_mode": "foreground"}}"#
                ))
            ),
        ]
    );
}

fn failed(text: &str) -> Reply {
    answer(&format!(
        r#"{{"result": {{"content": [{{"type": "text", "text": "{text}"}}], "isError": true, "structuredContent": {{"code": "tool_invocation_failed"}}}}}}"#
    ))
}

#[tokio::test(start_paused = true)]
async fn a_navigation_that_failed_for_a_network_error_is_its_own_outcome_not_a_loss() {
    let text = "navigation failed: net::ERR_TUNNEL_CONNECTION_FAILED";
    let (mut session, _script) = opened([failed(text)]).await;
    assert_eq!(
        session.navigate("https://www.python.org").await,
        Err(CuaError::refused("navigation-failed"))
    );
    // Only the Driver's exact error envelope: an answer not marked an error, an image, or an extra item is a loss.
    for result in [
        format!(r#"{{"content": [{{"type": "text", "text": "{text}"}}], "isError": false}}"#),
        format!(r#"{{"content": [{{"type": "text", "text": "{text}"}}]}}"#),
        format!(r#"{{"content": [{{"type": "image", "text": "{text}"}}], "isError": true}}"#),
        format!(r#"{{"content": [{{"type": "text", "text": "{text}"}}, "x"], "isError": true}}"#),
        format!(
            r#"{{"content": [{{"type": "text", "text": "{text}"}}, {{"type": "text", "text": "{text}"}}], "isError": true}}"#
        ),
    ] {
        let (mut session, _script) = opened([answer(&format!(r#"{{"result": {result}}}"#))]).await;
        assert_eq!(
            session.navigate("https://example.com").await,
            Err(CuaError::fatal("tool-failed"))
        );
    }
    // Any other failure text, or the same text on another action, may mean the Browser is lost.
    for other in [
        "navigation failed: something else",
        "navigation failed: net::ERR_X\\nmore",
        "navigation failed: net::ERR_",
        &format!("navigation failed: net::ERR_{}", "A".repeat(65)),
    ] {
        let (mut session, _script) = opened([failed(other)]).await;
        assert_eq!(
            session.navigate("https://example.com").await,
            Err(CuaError::fatal("tool-failed"))
        );
    }
    let (mut session, _script) = opened([failed(text)]).await;
    assert_eq!(
        session.click("p1:1").await,
        Err(CuaError::fatal("tool-failed"))
    );
}

#[tokio::test(start_paused = true)]
async fn only_an_explicitly_refused_page_action_is_not_fatal() {
    for result in [
        tool(r#"{"effect": "refused"}"#),
        tool(r#"{"status": "refused"}"#),
        tool_with(r#"{"effect": "refused"}"#, r#"{"isError": true}"#),
    ] {
        let (mut session, _script) = opened([result]).await;
        assert_eq!(
            session.click("p1:1").await,
            Err(CuaError::refused("action-refused"))
        );
    }
    // An error that names no refusal may have had an effect: the task must stop.
    for result in [
        tool_with(r#"{"x": 1}"#, r#"{"isError": true}"#),
        answer(r#"{"result": {}}"#),
        answer(r#"{"result": {"isError": true}}"#),
    ] {
        let (mut session, _script) = opened([result]).await;
        assert_eq!(
            session.click("p1:1").await,
            Err(CuaError::fatal("tool-failed"))
        );
    }
    let (mut session, _script) = opened([bound(), tool_with("{}", r#"{"isError": true}"#)]).await;
    assert_eq!(
        session.observe(false).await,
        Err(CuaError::fatal("tool-failed"))
    );
}

#[tokio::test(start_paused = true)]
async fn a_lapsed_grant_is_renewed_on_the_admitted_window_and_binding_goes_on() {
    for envelope in ["{}", r#"{"isError": true}"#] {
        // The refusal's own suggested call names another window: Team renews only the window it admitted.
        let (mut session, script) = opened([tool_with(LAPSED, envelope), tool(OK), bound()]).await;
        script.clear_sent();
        session.bind(None).await.expect("bound");
        assert_eq!(
            script.calls(),
            [
                ("get_browser_state".to_owned(), json(BIND)),
                ("browser_prepare".to_owned(), json(PREPARE)),
                ("get_browser_state".to_owned(), json(BIND)),
            ]
        );
    }
    // A grant that lapses again at once in the same binding is not renewed twice.
    let (mut session, _script) = opened([tool(LAPSED), tool(OK), tool(LAPSED)]).await;
    assert_eq!(
        session.bind(None).await,
        Err(CuaError::fatal("browser-binding-inexact").into())
    );
    // A renewal the Driver refuses is the attach refusal it always was.
    let (mut session, _script) = opened([tool(LAPSED), tool(r#"{"status": "refused"}"#)]).await;
    assert_eq!(
        session.bind(None).await,
        Err(CuaError::fatal("browser-attach-refused").into())
    );
    // Another consent refusal is no lapsed grant: it is never answered with a renewal, only read again.
    let other = LAPSED.replace("consumer_profile_endpoint_requires_grant", "user_denied");
    let (mut session, script) = opened([tool(&other), bound()]).await;
    session.bind(None).await.expect("bound");
    assert_eq!(
        script.names()[3..],
        ["get_browser_state", "get_browser_state"]
    );
}

#[tokio::test(start_paused = true)]
async fn an_old_grant_is_renewed_before_binding_within_the_same_bound() {
    let (mut session, script) = opened([tool(OK), bound(), bound()]).await;
    tokio::time::advance(REGRANT_SECONDS).await;
    session.bind(None).await.expect("bound");
    assert_eq!(
        script.names()[3..],
        ["browser_prepare", "get_browser_state"]
    );
    // A fresh grant binds without renewing.
    session.bind(None).await.expect("bound");
    assert_eq!(script.names()[5..], ["get_browser_state"]);
    // A deadline already spent never sends the renewal.
    session.set_deadline(Some(Instant::now()));
    tokio::time::advance(REGRANT_SECONDS).await;
    let sent = script.sent().len();
    assert_eq!(
        session.bind(None).await,
        Err(CuaError::fatal("deadline-passed").into())
    );
    assert_eq!(script.sent().len(), sent);
}

const PAGE: &str =
    r#"{"page": {"url": "https://example.com/", "title": "Example"}, "refs": [], "outline": "Hi"}"#;

#[tokio::test(start_paused = true)]
async fn a_grant_lapsed_at_capture_is_renewed_and_the_page_captured_once_more() {
    let (mut session, script) =
        opened([bound(), tool(LAPSED), tool(OK), bound(), tool(PAGE)]).await;
    let observation = session.observe(false).await.expect("observed");
    assert_eq!(observation.url, "https://example.com/");
    assert_eq!(
        script.names()[3..],
        [
            "get_browser_state",
            "get_browser_state",
            "browser_prepare",
            "get_browser_state",
            "get_browser_state"
        ]
    );
    let (mut session, _script) =
        opened([bound(), tool(LAPSED), tool(OK), bound(), tool(LAPSED)]).await;
    assert_eq!(
        session.observe(false).await,
        Err(CuaError::fatal("browser-binding-inexact"))
    );
}

const READ_REFUSED: &str = r#"{"status": "refused", "refusal": {"code": "browser_route_unavailable", "message": "frame gone"}}"#;

fn is_read(message: &Value) -> bool {
    message
        .get("params")
        .and_then(|params| params.get("arguments"))
        .and_then(|arguments| arguments.get("snapshot_format"))
        .is_some()
}

#[tokio::test(start_paused = true)]
async fn a_refused_page_read_is_asked_again_until_its_one_bound_is_spent() {
    for envelope in ["{}", r#"{"isError": true}"#] {
        let (mut session, _script) = opened([
            bound(),
            tool_with(READ_REFUSED, envelope),
            bound(),
            tool(PAGE),
        ])
        .await;
        let started = Instant::now();
        assert_eq!(
            session.observe(false).await.expect("read").url,
            "https://example.com/"
        );
        assert_eq!(started.elapsed(), READ_RETRY_SECONDS);
    }
    // A read still refused once no whole call fits in the read's bound is a loss.
    let spare = READ_SECONDS
        .checked_sub(CALL_SECONDS)
        .expect("a read outlasts a call");
    let reads = usize::try_from(spare.as_secs() / READ_RETRY_SECONDS.as_secs()).expect("small") + 1;
    let replies = (0..reads).flat_map(|_| [tool(READ_REFUSED), bound()]);
    let (mut session, script) = opened(std::iter::once(bound()).chain(replies)).await;
    assert_eq!(
        session.observe(false).await,
        Err(CuaError::fatal("browser-read-refused"))
    );
    assert_eq!(script.left(), 1);
}

#[tokio::test(start_paused = true)]
async fn a_slow_refused_read_is_asked_again_only_as_a_whole_call() {
    // A slow refusal leaves the next attempt a whole call within the read's bound, never a part of one.
    let (mut session, script) = opened([bound(), tool(READ_REFUSED), bound(), tool(PAGE)]).await;
    script.delay_sends(|message, _, deadline| {
        if is_read(message) {
            assert!(deadline - Instant::now() >= CALL_SECONDS);
            Duration::from_secs(20)
        } else {
            Duration::ZERO
        }
    });
    assert_eq!(
        session.observe(false).await.expect("read").url,
        "https://example.com/"
    );
    // A slow binding that leaves no whole call in the read's bound ends the read without asking it again.
    let (mut session, script) = opened([bound(), tool(READ_REFUSED), bound(), tool(PAGE)]).await;
    script.delay_sends(|message, count, _| {
        let arguments = message
            .get("params")
            .and_then(|params| params.get("arguments"));
        let rebind = arguments
            .and_then(|arguments| arguments.get("pid"))
            .is_some()
            && count > 4;
        if rebind || is_read(message) {
            Duration::from_secs(20)
        } else {
            Duration::ZERO
        }
    });
    assert_eq!(
        session.observe(false).await,
        Err(CuaError::fatal("browser-read-refused"))
    );
    assert_eq!(script.left(), 1);
}

#[tokio::test(start_paused = true)]
async fn a_refused_bind_is_asked_again_only_while_the_slowest_refusal_fits() {
    let refused = r#"{"status": "refused", "refusal": {"code": "authorization_host_failed", "message": "timed out"}}"#;
    // A refusal that came at once is asked again and the binding goes on.
    let (mut session, _script) =
        opened([tool_with(refused, r#"{"isError": true}"#), bound()]).await;
    session.bind(None).await.expect("bound");
    // One that took the Driver's whole wait leaves no room for another: Chromium refuses the read.
    let (mut session, script) = opened([tool_with(refused, r#"{"isError": true}"#)]).await;
    script.delay_sends(|_, _, _| REFUSAL_SECONDS.saturating_sub(Duration::from_secs(1)));
    assert_eq!(
        session.bind(None).await,
        Err(CuaError::fatal("browser-read-refused").into())
    );
    assert_eq!(script.left(), 0);
}

#[tokio::test(start_paused = true)]
async fn an_action_refused_for_a_lapsed_grant_renews_it_and_is_never_repeated() {
    let ended = format!(
        r#"{{"status": "refused", "refusal": {{"code": "{ENDED_CODE}", "message": "{ENDED_MESSAGE}"}}}}"#
    );
    for lapsed in [LAPSED, ended.as_str()] {
        let (mut session, script) =
            opened([tool_with(lapsed, r#"{"isError": true}"#), tool(OK)]).await;
        assert_eq!(
            session.click("p1:1").await,
            Err(CuaError::refused("action-refused"))
        );
        assert_eq!(script.names()[3..], ["browser_click", "browser_prepare"]);
    }
    // The same code with another message is an ordinary refusal: nothing is renewed.
    let other = format!(
        r#"{{"status": "refused", "refusal": {{"code": "{ENDED_CODE}", "message": "another reason"}}}}"#
    );
    let (mut session, script) = opened([tool_with(&other, r#"{"isError": true}"#)]).await;
    assert_eq!(
        session.click("p1:1").await,
        Err(CuaError::refused("action-refused"))
    );
    assert_eq!(script.names()[3..], ["browser_click"]);
}

#[tokio::test(start_paused = true)]
async fn a_failed_channel_is_a_fatal_loss_and_no_call_outlives_the_task() {
    let script = Script::default();
    script.break_receives();
    let mut session = CuaSession::new(script.channel());
    let deadline = Instant::now() + Duration::from_secs(10);
    session.set_deadline(Some(deadline));
    assert_eq!(
        session.request("initialize", json("{}"), None).await,
        Err(CuaError::fatal("channel-lost"))
    );
    assert_eq!(script.deadlines(), [deadline]);
    session.set_deadline(None);
    let now = Instant::now();
    assert!(
        session
            .request("initialize", json("{}"), None)
            .await
            .is_err()
    );
    assert_eq!(script.deadlines().last(), Some(&(now + CALL_SECONDS)));
    // Once the task's deadline passed, nothing more is sent.
    session.set_deadline(Some(Instant::now()));
    let sent = script.sent().len();
    assert_eq!(
        session.request("initialize", json("{}"), None).await,
        Err(CuaError::fatal("deadline-passed"))
    );
    assert_eq!(script.sent().len(), sent);
}

#[tokio::test(start_paused = true)]
async fn a_channel_that_refuses_the_initialized_notification_is_lost() {
    let script = Script::new([answer(r#"{"result": {"capabilities": {}}}"#)]);
    script.refuse_sends_containing("notifications/initialized");
    let mut session = CuaSession::new(script.channel());
    assert_eq!(session.open().await, Err(CuaError::fatal("channel-lost")));
}

#[tokio::test(start_paused = true)]
async fn the_protocol_admits_only_the_answer_to_the_current_request() {
    let script = Script::new([answer(r#"{"result": {"capabilities": {}}}"#)]);
    script.raw(br#"{"jsonrpc": "2.0", "method": "notifications/progress"}"#);
    let mut session = CuaSession::new(script.channel());
    assert!(
        session
            .request("initialize", json("{}"), None)
            .await
            .is_ok()
    );
    for (raw, code) in [
        (&b"\xff"[..], "protocol-invalid"),
        (b"{not json", "protocol-invalid"),
        (b"[]", "protocol-invalid"),
        (br#"{"id": 1, "id": 1, "result": {}}"#, "protocol-invalid"),
        (br#"{"id": 99, "result": {}}"#, "protocol-invalid"),
        (br#"{"id": 1, "error": {"code": -1}}"#, "protocol-error"),
        (br#"{"id": 1, "result": []}"#, "protocol-error"),
    ] {
        let script = Script::default();
        script.raw(raw);
        let mut session = CuaSession::new(script.channel());
        assert_eq!(
            session.request("initialize", json("{}"), None).await,
            Err(CuaError::fatal(code))
        );
    }
}
