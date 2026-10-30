//! Fixtures the Routine tests share (`routine_fixture.py`): a recorded one-step plan, confirmed definitions, and
//! claimed, bound, and held runs.

use jiff::civil::date;
use strict_json::Value;
use team_core::entropy::OsEntropy;

use crate::claim;
use crate::hold;
use crate::json::{at, elements, set, text};
use crate::plan;
use crate::record::{self, Claim, Lease, Notice, Routine, TeamRoutines};

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
/// The first step of a plan, by position.
pub const STEP: i64 = 1;
/// The default Routine's id.
pub const ROUTINE_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
/// The default Routine's description.
pub const DESCRIPTION: &str = "Checks the DNS zone.";

/// The epoch second of a UTC civil time.
pub fn epoch(year: i16, month: i8, day: i8, hour: i8, minute: i8, second: i8) -> i64 {
    date(year, month, day)
        .at(hour, minute, second, 0)
        .to_zoned(jiff::tz::TimeZone::UTC)
        .expect("a UTC instant")
        .timestamp()
        .as_second()
}

/// A JSON document a test writes.
pub fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("a test document")
}

/// 09:00 UTC on 1 October 2026.
pub fn nine() -> i64 {
    epoch(2026, 10, 1, 9, 0, 0)
}

/// The default anchor: 1 September 2026.
pub fn anchor() -> i64 {
    epoch(2026, 9, 1, 0, 0, 0)
}

/// A continuous schedule every hour.
pub fn every_hour() -> Value {
    json(r#"{"kind": "continuous", "gap": 3600}"#)
}

/// One well-formed one-step plan that runs `action` of `assistant` with no input, with this output.
pub fn plan_document(assistant: &str, timezone: &str, action: &str, output: Option<&str>) -> Value {
    json(&format!(
        r#"{{"version": 4, "timezone": "{timezone}",
            "steps": [{{"id": "check", "assistant": "{assistant}", "action": "{action}", "pin": "{PIN}", "input": {{}}}}],
            "output": {}}}"#,
        output.unwrap_or(r#"{"mode": "show", "step": "check"}"#)
    ))
}

/// The default one-step plan: `dns` `check`, shown.
pub fn default_plan() -> Value {
    plan_document("dns", "UTC", "check", None)
}

/// A plan of `count` copies of its one step, `s0` onward, with this output.
pub fn repeated_plan(count: usize, output: &str) -> Value {
    let mut plan = default_plan();
    let step = elements(at(&plan, "steps"))[0].clone();
    let steps = (0..count)
        .map(|index| {
            let mut copy = step.clone();
            set(&mut copy, "id", text(&format!("s{index}")));
            copy
        })
        .collect();
    set(&mut plan, "steps", Value::Array(steps));
    set(&mut plan, "output", json(output));
    plan
}

/// Every Action of `plan` once, at its step's pin, as a recording permits them.
pub fn permitted(plan: &Value, read_only: bool) -> Vec<Value> {
    let mut found: Vec<(String, String, String)> = elements(at(plan, "steps"))
        .iter()
        .map(|step| {
            let field = |key: &str| {
                crate::json::as_str(at(step, key))
                    .unwrap_or_default()
                    .to_owned()
            };
            (field("assistant"), field("action"), field("pin"))
        })
        .collect();
    found.sort();
    found.dedup_by(|left, right| (&left.0, &left.1) == (&right.0, &right.1));
    found
        .into_iter()
        .map(|(assistant, action, pin)| {
            crate::json::object(vec![
                ("assistant", text(&assistant)),
                ("action", text(&action)),
                ("pin", text(&pin)),
                ("read_only", Value::Bool(read_only)),
                ("stored_inputs", Value::Array(Vec::new())),
            ])
        })
        .collect()
}

/// The confirmation of every fixture Routine.
pub fn confirmation() -> Value {
    json(&format!(
        r#"{{"proposal_id": "{}", "proposal_digest": "sha256:{}", "principal": "{}", "incarnation": "{}", "confirmed_at": 1}}"#,
        "c".repeat(32),
        "e".repeat(64),
        "f".repeat(32),
        "b".repeat(64)
    ))
}

/// A Routine record with its plan's permitted Actions, its Assistants pinned, a description, and a confirmation.
pub fn confirmed(value: Routine) -> Routine {
    let mut value = value;
    let entries = permitted(&value.plan, true);
    let mut assistants: Vec<String> = entries
        .iter()
        .filter_map(|item| crate::json::as_str(at(item, "assistant")).map(str::to_owned))
        .collect();
    assistants.sort();
    assistants.dedup();
    if value.description.is_empty() {
        value.description = DESCRIPTION.to_owned();
    }
    value.permitted = entries;
    value.assistants = assistants
        .into_iter()
        .map(|assistant| (assistant, SCOPE_PIN.to_owned()))
        .collect();
    value.confirmation = Some(confirmation());
    value
}

/// A confirmed Routine: by default the one-step plan daily at 09:00, due when it first fires after its anchor.
pub struct Fixture {
    /// Its id.
    pub id: String,
    /// Its schedule.
    pub schedule: Value,
    /// Its name.
    pub name: &'static str,
    /// Its anchor.
    pub anchor: i64,
    /// Its plan.
    pub plan: Value,
}

impl Default for Fixture {
    fn default() -> Self {
        Self {
            id: ROUTINE_A.to_owned(),
            schedule: json(r#"{"kind": "daily", "time": "09:00"}"#),
            name: "Daily DNS summary",
            anchor: anchor(),
            plan: default_plan(),
        }
    }
}

impl Fixture {
    /// The confirmed Routine, next due at its first firing.
    pub fn build(self) -> Routine {
        let mut value = confirmed(Routine::new(
            &self.id,
            self.name,
            self.schedule,
            Vec::new(),
            self.plan,
            self.anchor,
            0,
        ));
        value.next_run_at = record::first_run(&value).expect("a first firing");
        value
    }
}

/// The default Routine.
pub fn routine() -> Routine {
    Fixture::default().build()
}

/// The default Routine of another id.
pub fn routine_of(id: &str) -> Routine {
    Fixture {
        id: id.to_owned(),
        ..Fixture::default()
    }
    .build()
}

/// The default Routine of another id and schedule.
pub fn routine_scheduled(id: &str, schedule: Value) -> Routine {
    Fixture {
        id: id.to_owned(),
        schedule,
        ..Fixture::default()
    }
    .build()
}

/// A Team's Routine state holding these Routines and nothing else.
pub fn added(routines: &[Routine]) -> TeamRoutines {
    let mut state = TeamRoutines::default();
    for value in routines {
        state = record::add_routine(&state, value).expect("an added Routine");
    }
    state
}

/// The state with one Routine next due at `next_run_at`.
pub fn at_time(state: &TeamRoutines, routine_id: &str, next_run_at: i64) -> TeamRoutines {
    let mut next = state.clone();
    let mut value = record::routine(&next, routine_id)
        .expect("a Routine")
        .clone();
    value.next_run_at = next_run_at;
    record::replace_routine(&mut next, value);
    next
}

/// Changes one Routine of the state in place.
pub fn edit(
    state: &TeamRoutines,
    routine_id: &str,
    change: impl FnOnce(&mut Routine),
) -> TeamRoutines {
    let mut next = state.clone();
    let mut value = record::routine(&next, routine_id)
        .expect("a Routine")
        .clone();
    change(&mut value);
    record::replace_routine(&mut next, value);
    next
}

/// Changes one run of the state in place.
pub fn edit_run(
    state: &TeamRoutines,
    run_id: &str,
    change: impl FnOnce(&mut record::Run),
) -> TeamRoutines {
    let mut next = state.clone();
    let mut value = record::run(&next, run_id).expect("a run").clone();
    change(&mut value);
    record::replace_run(&mut next, value);
    next
}

/// Claim the Team's due run at `now` and bind its first generation, as its first segment does.
pub fn claim_bound(state: &TeamRoutines, now: i64, network: &str) -> (TeamRoutines, Claim, Lease) {
    let (state, claim) = claim::claim(state, now, KEY, true, &ENTROPY).expect("a claim");
    let claim = claim.expect("a due run");
    let lease = record::lease_of(&claim.lease_token, KEY);
    let state = claim::bind_generation(&state, &claim.run.run_id, &lease, now, network)
        .expect("a bound generation");
    (state, claim, lease)
}

/// The default Routine due at nine, claimed at `now`: the state, the claim, and its lease.
pub fn claimed_at(now: i64) -> (TeamRoutines, Claim, Lease) {
    let due = at_time(&added(&[routine()]), ROUTINE_A, nine());
    let (state, claim) = claim::claim(&due, now, KEY, true, &ENTROPY).expect("a claim");
    let claim = claim.expect("a due run");
    let lease = record::lease_of(&claim.lease_token, KEY);
    (state, claim, lease)
}

/// The default Routine claimed at nine.
pub fn claimed() -> (TeamRoutines, Claim, Lease) {
    claimed_at(nine())
}

/// The claimed default run with its first generation bound in network `net_1`.
pub fn bound() -> (TeamRoutines, Claim, Lease) {
    let (state, claim, lease) = claimed();
    let state = claim::bind_generation(&state, &claim.run.run_id, &lease, nine(), "net_1")
        .expect("a bound generation");
    (state, claim, lease)
}

/// The bound default run fenced for a hold, and its run id.
pub fn held() -> (TeamRoutines, String) {
    let (state, claim, lease) = bound();
    let state = hold::fence(&state, &claim.run.run_id, &lease, nine()).expect("a fenced run");
    (state, claim.run.run_id)
}

/// A completed run's notice detail: the summary of the plan it carried out.
pub fn done_detail() -> Value {
    json(&format!(
        r#"{{"plan": {{"revision": 1, "plan_digest": "sha256:{}", "steps": 1, "actions": [["dns", "check", 1]],
              "more": 0}}, "output": null}}"#,
        "d".repeat(64)
    ))
}

/// A full queue of undelivered done notices of another Routine.
pub fn full_notices(count: usize) -> Vec<Notice> {
    (0..count)
        .map(|index| {
            Notice::new(
                &format!("{index:032x}"),
                &"c".repeat(32),
                "",
                "done",
                nine(),
                done_detail(),
            )
        })
        .collect()
}

/// A completed run's detail at about its largest: a shown output near its 16 KiB bound and a full summary.
pub fn large_completion() -> Value {
    let mut plan = default_plan();
    let step = elements(at(&plan, "steps"))[0].clone();
    let steps = (0..256)
        .map(|index| {
            let mut copy = step.clone();
            set(&mut copy, "id", text(&format!("s{index}")));
            set(
                &mut copy,
                "action",
                text(if index % 2 == 1 { "check" } else { "notify" }),
            );
            copy
        })
        .collect();
    set(&mut plan, "steps", Value::Array(steps));
    let result = Value::Object(
        (0..24)
            .map(|index| (format!("k{index:02}"), text(&"\u{1}".repeat(300))))
            .collect(),
    );
    let node = plan::output_safe(&result, &json("{}"), &[]).expect("a safe form");
    crate::json::object(vec![
        ("plan", crate::definition::summary(&plan, 1)),
        ("output", plan::output_shown(&crate::json::int(256), &node)),
    ])
}
