//! `test_routine_schedule.py`: schedules fire in the Routine's zone, strictly after an instant, across daylight-saving
//! changes.

use jiff::Timestamp;
use strict_json::Value;
use team_protocol::routine::{MAX_CONTINUOUS_CAP, canonical_schedule, continuous_cap, daily_cap};

use super::{next_run, zone, zone_named};
use crate::testing::{epoch, json};

const NEW_YORK: &str = "America/New_York";

fn anchor() -> i64 {
    epoch(2026, 1, 1, 0, 0, 0)
}

fn name(zone: &str) -> Value {
    Value::String(zone.to_owned())
}

fn local(instant: i64, zone: &str) -> (i16, i8, i8, i8, i8) {
    let zoned = Timestamp::from_second(instant)
        .expect("an instant")
        .to_zoned(zone_named(zone).expect("a zone"));
    (
        zoned.year(),
        zoned.month(),
        zoned.day(),
        zoned.hour(),
        zoned.minute(),
    )
}

fn upcoming(schedule: &Value, timezone: &str, after: i64, count: usize) -> Vec<i64> {
    let mut runs = Vec::new();
    let mut after = after;
    for _ in 0..count {
        after = next_run(schedule, &name(timezone), anchor(), after).expect("a firing");
        runs.push(after);
    }
    runs
}

#[test]
fn only_the_closed_schedule_shapes_are_canonical() {
    for valid in [
        r#"{"kind": "continuous", "gap": 3600}"#,
        r#"{"kind": "daily", "time": "00:00"}"#,
        r#"{"kind": "weekly", "weekday": 6, "time": "23:59"}"#,
        r#"{"kind": "monthly", "day": 28, "time": "09:05"}"#,
    ] {
        assert!(canonical_schedule(&json(valid)).is_some(), "{valid}");
    }
    for invalid in [
        "null",
        r#"{"kind": "yearly", "time": "09:00"}"#,
        r#"{"kind": "continuous", "gap": 4}"#,
        r#"{"kind": "continuous", "gap": 5.0}"#,
        r#"{"kind": "daily", "time": "24:00"}"#,
        r#"{"kind": "daily", "time": "9:00"}"#,
        r#"{"kind": "daily", "time": "09:00", "every": 1}"#,
        r#"{"kind": "weekly", "weekday": 7, "time": "09:00"}"#,
        r#"{"kind": "monthly", "day": 29, "time": "09:00"}"#,
    ] {
        assert!(canonical_schedule(&json(invalid)).is_none(), "{invalid}");
    }
    assert_eq!(
        daily_cap(&json(r#"{"kind": "continuous", "gap": 21600}"#)),
        4
    );
    assert_eq!(daily_cap(&json(r#"{"kind": "daily", "time": "09:00"}"#)), 1);
    for (gap, cap) in [
        (5, MAX_CONTINUOUS_CAP),
        (7, 12_343),
        (30, 2880),
        (86_400, 1),
        (366 * 86_400, 1),
    ] {
        assert_eq!(continuous_cap(gap), cap);
    }
}

#[test]
fn timezones_are_iana_names_that_load_never_paths() {
    for zone_name in [
        "UTC",
        "America/Sao_Paulo",
        "America/Argentina/Buenos_Aires",
        "Etc/GMT+3",
    ] {
        let loaded = zone(&name(zone_name)).expect("a zone");
        assert_eq!(loaded.iana_name(), Some(zone_name));
    }
    for refused in [
        "",
        "/etc/localtime",
        "../UTC",
        "America//Sao_Paulo",
        "Mars/Olympus",
        "america/sao_paulo",
    ] {
        assert!(zone(&name(refused)).is_err(), "{refused}");
    }
    assert!(zone(&json("3")).is_err());
}

#[test]
fn each_kind_fires_strictly_after_the_instant() {
    let weekly = json(r#"{"kind": "weekly", "weekday": 0, "time": "09:00"}"#);
    assert_eq!(
        upcoming(&weekly, "America/Sao_Paulo", epoch(2026, 9, 30, 0, 0, 0), 3),
        vec![
            epoch(2026, 10, 5, 12, 0, 0),
            epoch(2026, 10, 12, 12, 0, 0),
            epoch(2026, 10, 19, 12, 0, 0)
        ]
    );
    let monthly = json(r#"{"kind": "monthly", "day": 28, "time": "23:30"}"#);
    assert_eq!(
        upcoming(&monthly, "UTC", epoch(2026, 1, 28, 23, 30, 0), 2),
        vec![epoch(2026, 2, 28, 23, 30, 0), epoch(2026, 3, 28, 23, 30, 0)]
    );
    let daily = json(r#"{"kind": "daily", "time": "09:00"}"#);
    let utc = name("UTC");
    assert_eq!(
        next_run(&daily, &utc, anchor(), epoch(2026, 9, 30, 8, 59, 0)),
        Ok(epoch(2026, 9, 30, 9, 0, 0))
    );
    assert_eq!(
        next_run(&daily, &utc, anchor(), epoch(2026, 9, 30, 9, 0, 0)),
        Ok(epoch(2026, 10, 1, 9, 0, 0))
    );
}

#[test]
fn a_continuous_run_is_due_its_gap_after_an_instant_never_before_the_anchor() {
    let continuous = json(r#"{"kind": "continuous", "gap": 5}"#);
    let utc = name("UTC");
    assert_eq!(
        next_run(&continuous, &utc, anchor(), epoch(2026, 9, 30, 0, 0, 0)),
        Ok(epoch(2026, 9, 30, 0, 0, 5))
    );
    assert_eq!(
        next_run(
            &continuous,
            &utc,
            epoch(2026, 10, 1, 0, 0, 0),
            epoch(2026, 9, 30, 0, 0, 0)
        ),
        Ok(epoch(2026, 10, 1, 0, 0, 5))
    );
}

#[test]
fn a_nonexistent_local_time_runs_shifted_past_the_gap_and_an_ambiguous_one_first() {
    let daily = json(r#"{"kind": "daily", "time": "02:30"}"#);
    let spring = next_run(
        &daily,
        &name(NEW_YORK),
        anchor(),
        epoch(2026, 3, 8, 0, 0, 0),
    )
    .expect("a firing");
    assert_eq!(local(spring, NEW_YORK), (2026, 3, 8, 3, 30));
    let following = next_run(&daily, &name(NEW_YORK), anchor(), spring).expect("a firing");
    assert_eq!(local(following, NEW_YORK).3, 2);
    let fall = next_run(
        &json(r#"{"kind": "daily", "time": "01:30"}"#),
        &name(NEW_YORK),
        anchor(),
        epoch(2026, 11, 1, 0, 0, 0),
    );
    assert_eq!(fall, Ok(epoch(2026, 11, 1, 5, 30, 0)));
}

#[test]
fn invalid_input_is_refused() {
    let invalid = next_run(
        &json(r#"{"kind": "daily", "time": "25:00"}"#),
        &name("UTC"),
        anchor(),
        anchor(),
    );
    assert_eq!(invalid.map_err(|error| error.0), Err("invalid schedule"));
}
