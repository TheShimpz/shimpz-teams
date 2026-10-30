//! A completed turn's terminal answer.

use strict_json::Value;
use team_inference::usage::Spent;
use team_protocol::python::value::object_of;

use crate::orchestrator::ChatOutcome;

/// A completed turn's reply body, with the whole turn's usage, measured once, when it was metered.
#[must_use]
pub fn terminal_body(
    team_id: &str,
    team_name: &str,
    outcome: &ChatOutcome,
    usage: Option<&Spent>,
) -> Value {
    let mut members = vec![
        ("team_id", Value::String(team_id.to_owned())),
        ("team_name", Value::String(team_name.to_owned())),
        ("reply", Value::String(outcome.reply.clone())),
        (
            "clarification",
            outcome.clarification.clone().unwrap_or(Value::Null),
        ),
    ];
    if let Some(wire) = usage.and_then(Spent::wire) {
        members.push(("usage", wire));
    }
    if let Some(browser) = &outcome.browser {
        members.push(("browser", browser.clone()));
    }
    object_of(members)
}

#[cfg(test)]
mod tests {
    use strict_json::Value;
    use team_inference::usage::Spent;

    use super::terminal_body;
    use crate::orchestrator::ChatOutcome;

    #[test]
    fn the_terminal_carries_usage_only_when_metered_and_the_browser_only_when_it_ran() {
        let outcome = ChatOutcome {
            reply: "Done.".to_owned(),
            actions: Vec::new(),
            clarification: None,
            memory: Vec::new(),
            routine: None,
            browser: None,
        };
        let plain = terminal_body("t", "Team", &outcome, Some(&Spent::default()));
        assert_eq!(
            plain,
            strict_json::parse(
                br#"{"team_id":"t","team_name":"Team","reply":"Done.","clarification":null}"#
            )
            .expect("json")
        );
        let spent = Spent {
            duration_ms: 5,
            models: vec![("openai".to_owned(), "gpt-6-luna".to_owned(), 1, 2)],
        };
        let browsed = ChatOutcome {
            browser: Some(Value::Bool(true)),
            ..outcome
        };
        let body = terminal_body("t", "Team", &browsed, Some(&spent));
        assert!(body.get("usage").is_some());
        assert_eq!(body.get("browser"), Some(&Value::Bool(true)));
    }
}
