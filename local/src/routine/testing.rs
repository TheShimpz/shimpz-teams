//! Fixtures the Local Routine tests share (`tests/routine_fixture.py`): a recorded one-step plan, confirmed
//! definitions, and claimed, bound, frozen, held, and ended runs in a 64-hex Team network.

use std::sync::Arc;

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_routine::record::{self, Claim, Lease, Notice, Routine, TeamRoutines};
use team_routine::{claim, definition, hold, plan, runs};

/// Every id and token a test mints.
pub static ENTROPY: OsEntropy = OsEntropy;
/// The pin of every step of the fixture plans.
pub const PIN: &str = "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
/// The scope pin of every Assistant of the fixture Routines.
pub const SCOPE_PIN: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
/// The routine key fingerprint runs are claimed under.
pub const KEY: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
/// The Team network their generations are bound in.
pub const NETWORK: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
/// 09:00 UTC on 1 October 2026.
pub const NINE: i64 = 1_790_845_200;

/// The shared entropy, as a store takes it.
pub fn entropy() -> Arc<dyn team_core::entropy::Entropy> {
    Arc::new(OsEntropy)
}

/// A JSON document a test writes.
pub fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("a test document")
}

/// One well-formed one-step plan that runs `dns` `check` with no input in `timezone`, with this output.
pub fn plan_document(timezone: &str, output: Option<&str>) -> Value {
    json(&format!(
        r#"{{"version": 4, "timezone": "{timezone}",
            "steps": [{{"id": "check", "assistant": "dns", "action": "check", "pin": "{PIN}", "input": {{}}}}],
            "output": {}}}"#,
        output.unwrap_or(r#"{"mode": "show", "step": "check"}"#)
    ))
}

/// A Routine with its plan's permitted Action, its Assistant pinned, a description, and a confirmation.
pub fn confirmed(mut value: Routine) -> Routine {
    value.permitted = vec![json(&format!(
        r#"{{"assistant": "dns", "action": "check", "pin": "{PIN}", "read_only": true, "stored_inputs": []}}"#
    ))];
    value.assistants = vec![("dns".to_owned(), SCOPE_PIN.to_owned())];
    if value.description.is_empty() {
        value.description = "Checks the DNS zone.".to_owned();
    }
    value.confirmation = Some(json(&format!(
        r#"{{"proposal_id": "{}", "proposal_digest": "sha256:{}", "principal": "{}", "incarnation": "{}", "confirmed_at": 1}}"#,
        "c".repeat(32),
        "e".repeat(64),
        "f".repeat(32),
        "b".repeat(64)
    )));
    value
}

/// A confirmed daily-09:00 Routine of `id` in `timezone`, named `name`, due at its first firing after `anchor`.
pub fn routine_in(id: &str, name: &str, anchor: i64, timezone: &str) -> Routine {
    let mut value = confirmed(Routine::new(
        id,
        name,
        json(r#"{"kind": "daily", "time": "09:00"}"#),
        Vec::new(),
        plan_document(timezone, None),
        anchor,
        0,
    ));
    value.next_run_at = record::first_run(&value).expect("a first firing");
    value
}

/// The store tests' Routine: "Resumo de DNS" in São Paulo, anchored a day before nine.
pub fn routine(id: &str) -> Routine {
    routine_in(id, "Resumo de DNS", NINE - 86_400, "America/Sao_Paulo")
}

/// A Routine that shows changes, continuously every hour, with a rolled-up usage.
pub fn changes_routine(id: &str) -> Routine {
    let mut value = routine(id);
    value.plan = plan_document("UTC", Some(r#"{"mode": "changes", "step": "check"}"#));
    value.schedule = json(r#"{"kind": "continuous", "gap": 3600}"#);
    value.rollup_usage = Some(json(r#"{"duration_ms": 5}"#));
    value.next_run_at = record::first_run(&value).expect("a first firing");
    value
}

/// A Team's state holding these Routines and nothing else.
pub fn added(routines: &[Routine]) -> TeamRoutines {
    let mut state = TeamRoutines::default();
    for value in routines {
        state = record::add_routine(&state, value).expect("an added Routine");
    }
    state
}

/// Claim the Team's due run at `now` and bind its first generation in [`NETWORK`].
pub fn claim_bound(state: &TeamRoutines, now: i64) -> (TeamRoutines, Claim, Lease) {
    let (state, claim) = claim::claim(state, now, KEY, true, &ENTROPY).expect("a claim");
    let claim = claim.expect("a due run");
    let lease = record::lease_of(&claim.lease_token, KEY);
    let state = claim::bind_generation(&state, &claim.run.run_id, &lease, now, NETWORK)
        .expect("a bound generation");
    (state, claim, lease)
}

/// Every kind of run, notice, and queued removal, as a real Team accumulates them.
pub fn busy_state() -> TeamRoutines {
    let state = added(&[
        routine(&"a".repeat(32)),
        routine(&"b".repeat(32)),
        routine(&"e".repeat(32)),
    ]);
    let now = routine(&"a".repeat(32)).next_run_at + 60;
    let (state, first, lease) = claim_bound(&state, now);
    let request = runs::Request {
        kind: "human".to_owned(),
        assistant_id: "dns".to_owned(),
        action: "check".to_owned(),
        position: 1,
    };
    let state = runs::freeze(&state, &first.run.run_id, &lease, now, &request, &ENTROPY)
        .expect("a frozen run");
    let (state, second, lease) = claim_bound(&state, now);
    let state = hold::fence(&state, &second.run.run_id, &lease, now).expect("a held run");
    let (state, third, _lease) = claim_bound(&state, now);
    runs::end(
        &state,
        &third.run.run_id,
        now,
        "stopped",
        json("{}"),
        None,
        &ENTROPY,
    )
    .expect("an ended run")
}

/// A completed run's notice detail at about its largest: a shown output near its bound and a full summary.
pub fn large_completion() -> Value {
    let mut steps = Vec::new();
    for index in 0..256 {
        let action = if index % 2 == 1 { "check" } else { "notify" };
        steps.push(json(&format!(
            r#"{{"id": "s{index}", "assistant": "dns", "action": "{action}", "pin": "{PIN}", "input": {{}}}}"#
        )));
    }
    let mut document = plan_document("UTC", None);
    if let Value::Object(members) = &mut document {
        for member in members.iter_mut() {
            if member.0 == "steps" {
                member.1 = Value::Array(steps.clone());
            }
        }
    }
    let result = Value::Object(
        (0..24)
            .map(|index| (format!("k{index:02}"), Value::String("\u{1}".repeat(300))))
            .collect(),
    );
    let node = plan::output_safe(&result, &json("{}"), &[]).expect("a safe form");
    Value::Object(vec![
        ("plan".to_owned(), definition::summary(&document, 1)),
        ("output".to_owned(), plan::output_shown(&json("256"), &node)),
    ])
}

/// A done notice of Routine `a…` with a large detail, the given name, usage, and protection.
pub fn large_notice(index: usize, name: &str) -> Notice {
    let mut notice = Notice::new(
        &format!("{index:032x}"),
        &"a".repeat(32),
        &format!("{index:032x}"),
        "done",
        NINE,
        large_completion(),
    );
    notice.name = name.to_owned();
    notice.usage = Some(json(r#"{"duration_ms": 1200}"#));
    notice.protection_lost = true;
    notice
}
