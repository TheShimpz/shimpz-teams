use std::time::Duration;

use strict_json::Value;
use team_core::exec::ExecExit;
use team_core::stop::Stop;
use team_core::testing::{FakeExec, ScriptedExec};
use team_protocol::cause::{Cause, Cleanup, Condition, Phase};
use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};
use tokio::time::Instant;

use super::{
    DockerSlots, OPERATION_ID_PLACEHOLDER, RpcCall, RpcError, RpcFailure, encode_invocation,
    exchange, routine_condition, terminal,
};
use crate::frames::tests::frame;

const OPERATION: &str = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6";

fn object(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

/// Scripts one exec answering `output` (stdout frames) and exiting `code`.
fn scripted(engine: &FakeExec, output: Vec<u8>, code: Option<i64>) {
    let (ours, mut workload) = duplex(64 * 1024);
    tokio::spawn(async move {
        let mut request = Vec::new();
        let _ = workload.read_to_end(&mut request).await;
        let _ = workload.write_all(&output).await;
        let _ = workload.shutdown().await;
    });
    engine.push(ScriptedExec {
        stream: ours,
        exit: ExecExit {
            running: code.is_none(),
            exit_code: code,
        },
    });
}

async fn run(
    engine: &FakeExec,
    slots: &DockerSlots,
    stop: &Stop,
    deadline: Instant,
) -> Result<Value, RpcError> {
    let call = RpcCall {
        container: "container",
        action: "lookup",
        encoded: b"{}",
        deadline,
        stop,
        broker: None,
    };
    exchange(engine, slots, call).await
}

fn soon() -> Instant {
    Instant::now() + Duration::from_secs(5)
}

#[test]
fn stored_input_ids_only_in_exec_request() {
    let encoded = encode_invocation(
        &object(r#"{"q":"é"}"#),
        &["b-key".to_owned(), "a-key".to_owned()],
        &object("{}"),
        OPERATION,
        &[],
    )
    .expect("encoded");
    let expected = format!(
        r#"{{"input":{{"q":"\u00e9"}},"stored_inputs":["a-key","b-key"],"files":{{}},"operation_id":"{OPERATION}"}}"#
    );
    assert_eq!(String::from_utf8(encoded).expect("ascii"), expected);
    let replay = encode_invocation(
        &object("{}"),
        &[],
        &object("{}"),
        OPERATION,
        &[object(r#"{"kind":"approval"}"#)],
    )
    .expect("encoded");
    assert!(
        String::from_utf8(replay)
            .expect("ascii")
            .ends_with(r#""responses":[{"kind":"approval"}]}"#)
    );
    assert!(
        encode_invocation(
            &object("{}"),
            &[],
            &object("{}"),
            &OPERATION.to_uppercase(),
            &[]
        )
        .is_err()
    );
    assert!(
        encode_invocation(
            &object("{}"),
            &[],
            &object("{}"),
            OPERATION_ID_PLACEHOLDER,
            &[]
        )
        .is_ok()
    );
}

#[test]
fn only_delivered_file_content_admits_the_larger_invocation_bound() {
    let large = Value::String("x".repeat(600 * 1024));
    let withheld =
        object(r#"{"0123456789abcdef0123456789abcdef":{"content":{"type":"withheld"}}}"#);
    assert!(encode_invocation(&large, &[], &withheld, OPERATION, &[]).is_err());
    let delivered =
        object(r#"{"0123456789abcdef0123456789abcdef":{"content":{"type":"delivered"}}}"#);
    assert!(encode_invocation(&large, &[], &delivered, OPERATION, &[]).is_ok());
}

#[tokio::test]
async fn a_clean_exit_returns_the_terminal_object_from_the_exact_exec() {
    let engine = FakeExec::default();
    scripted(
        &engine,
        frame(1, br#"{"type":"result","result":{}}"#),
        Some(0),
    );
    let answer = run(&engine, &DockerSlots::default(), &Stop::new(), soon()).await;
    assert_eq!(answer, Ok(object(r#"{"type":"result","result":{}}"#)));
    let started = &engine.started()[0];
    assert_eq!(
        (started.argv.as_slice(), started.user.as_str()),
        (
            &[
                "/usr/local/bin/shimpz-action".to_owned(),
                "lookup".to_owned()
            ][..],
            "10001:10001"
        )
    );
    assert_eq!(
        (
            started.working_dir.as_deref(),
            started.env.is_empty(),
            started.stdin
        ),
        (Some("/tmp"), true, true)
    );
    assert_eq!(engine.stopped().len(), 0);
}

const SECRET: &str = "sk-live-Rpc-9f1c2b";

#[tokio::test]
async fn transport_faults_fail_stop_and_keep_each_exact_cause_without_child_output() {
    let stderr = format!("Traceback: {SECRET}");
    let stopped = |cause: Cause| cause.with_cleanup(Cleanup::WorkloadStopped);
    let cases = [
        (
            frame(1, b"{}"),
            Some(1),
            stopped(Cause::exit_status(1)),
            "exit-status:1",
        ),
        (
            [frame(1, b"{}"), frame(2, stderr.as_bytes())].concat(),
            Some(1),
            stopped(Cause::exit_status(1).with_stderr(stderr.len())),
            "exit-status:1",
        ),
        (
            [frame(1, b"{}"), frame(2, stderr.as_bytes())].concat(),
            Some(0),
            stopped(Cause::stderr_output(stderr.len())),
            "stderr-output",
        ),
        (
            frame(1, b"{}"),
            None,
            stopped(Cause::new(Condition::StillRunning, Phase::ExitInspection)),
            "exit-unavailable",
        ),
        (
            b"garbage!".to_vec(),
            Some(0),
            stopped(Cause::new(Condition::StreamUnknown, Phase::Exchange)),
            "frame-invalid",
        ),
    ];
    for (output, code, expected, routine) in cases {
        let engine = FakeExec::default();
        scripted(&engine, output, code);
        let Err(RpcError::Failed { cause, .. }) =
            run(&engine, &DockerSlots::default(), &Stop::new(), soon()).await
        else {
            panic!("a failure");
        };
        assert_eq!((cause, engine.stopped().len()), (expected, 1));
        assert_eq!(routine_condition(&cause), routine);
        let rendered = format!("{} {:?}", cause.render(), cause.to_value());
        assert!(!rendered.contains(SECRET) && !rendered.contains("Traceback"));
    }
}

#[tokio::test]
async fn an_exec_that_cannot_start_is_fail_stopped_and_says_so() {
    let engine = FakeExec::default();
    let outcome = run(&engine, &DockerSlots::default(), &Stop::new(), soon()).await;
    assert_eq!(
        outcome,
        Err(RpcError::Failed {
            kind: RpcFailure::Failed,
            cause: Cause::new(Condition::ExecStartFailed, Phase::ExecStart)
                .with_cleanup(Cleanup::WorkloadStopped),
        })
    );
}

#[tokio::test]
async fn exec_fail_stop_proves_not_running_or_blocks_keeping_the_initiating_cause() {
    let engine = FakeExec::default();
    engine.block_fail_stop();
    scripted(&engine, frame(1, b"{}"), Some(3));
    assert_eq!(
        run(&engine, &DockerSlots::default(), &Stop::new(), soon()).await,
        Err(RpcError::Blocked(
            Cause::exit_status(3).with_cleanup(Cleanup::WorkloadStopUnconfirmed)
        ))
    );
}

#[tokio::test]
async fn a_stopped_turn_or_saturated_pool_never_dispatches() {
    let engine = FakeExec::default();
    let stop = Stop::new();
    stop.stop();
    assert_eq!(
        run(&engine, &DockerSlots::default(), &stop, soon()).await,
        Err(RpcError::NotDispatched(Cause::new(
            Condition::Stopped,
            Phase::SlotAdmission
        )))
    );
    let saturated = DockerSlots::with(0);
    let deadline = Instant::now() + Duration::from_millis(50);
    assert_eq!(
        run(&engine, &saturated, &Stop::new(), deadline).await,
        Err(RpcError::NotDispatched(Cause::new(
            Condition::SlotTimeout,
            Phase::SlotAdmission
        )))
    );
    assert_eq!((engine.started().len(), engine.stopped().len()), (0, 0));
}

#[tokio::test]
async fn a_workload_that_never_answers_times_out_and_is_fail_stopped() {
    let engine = FakeExec::default();
    let (ours, workload) = duplex(1024);
    engine.push(ScriptedExec {
        stream: ours,
        exit: ExecExit {
            running: true,
            exit_code: None,
        },
    });
    let deadline = Instant::now() + Duration::from_millis(100);
    let Err(RpcError::Failed { kind, cause }) =
        run(&engine, &DockerSlots::default(), &Stop::new(), deadline).await
    else {
        panic!("a timeout");
    };
    drop(workload);
    assert_eq!(
        (kind, cause, engine.stopped().len()),
        (
            RpcFailure::Timeout,
            Cause::new(Condition::DeadlinePassed, Phase::Exchange)
                .with_cleanup(Cleanup::WorkloadStopped),
            1
        )
    );
    assert_eq!(routine_condition(&cause), "timeout");
    assert_eq!(
        (RpcFailure::Timeout.status(), RpcFailure::Ambiguous.status()),
        (504, 502)
    );
}

#[test]
fn a_request_after_a_provider_call_is_refused() {
    let request = br#"{"type":"request","request":{}}"#;
    assert!(terminal(request, 0).is_ok());
    let refused = terminal(request, 1);
    let invalid = |condition| RpcError::Failed {
        kind: RpcFailure::InvalidResult,
        cause: Cause::new(condition, Phase::ResultValidation),
    };
    assert_eq!(refused, Err(invalid(Condition::RequestAfterProviderCall)));
    assert!(terminal(br#"{"type":"result","result":{}}"#, 1).is_ok());
    for (raw, condition) in [
        (b"{".as_slice(), Condition::LineNotJson),
        (b"[]", Condition::TerminalNotObject),
    ] {
        assert_eq!(terminal(raw, 0), Err(invalid(condition)));
    }
}

/// Runs one exchange whose workload never answers, stopped 20 ms in.
async fn stopped_mid_exchange(engine: &FakeExec) -> Result<Value, RpcError> {
    let (ours, workload) = duplex(1024);
    engine.push(ScriptedExec {
        stream: ours,
        exit: ExecExit {
            running: true,
            exit_code: None,
        },
    });
    let stop = Stop::new();
    let stopping = stop.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(20)).await;
        stopping.stop();
    });
    let outcome = run(engine, &DockerSlots::default(), &stop, soon()).await;
    drop(workload);
    outcome
}

#[tokio::test]
async fn a_stop_while_the_action_runs_fail_stops_its_workload_and_leaves_it_uncertain() {
    let engine = FakeExec::default();
    let started = Instant::now();
    let outcome = stopped_mid_exchange(&engine).await;
    assert!(
        started.elapsed() < Duration::from_secs(4),
        "Stop, not the deadline, ended it"
    );
    assert_eq!(
        (outcome, engine.started().len(), engine.stopped().len()),
        (
            Err(RpcError::Failed {
                kind: RpcFailure::Ambiguous,
                cause: Cause::new(Condition::Stopped, Phase::Exchange)
                    .with_cleanup(Cleanup::WorkloadStopped),
            }),
            1,
            1
        )
    );
}

#[tokio::test]
async fn a_stopped_workload_that_cannot_be_proven_stopped_is_blocked() {
    let engine = FakeExec::default();
    engine.block_fail_stop();
    assert_eq!(
        stopped_mid_exchange(&engine).await,
        Err(RpcError::Blocked(
            Cause::new(Condition::Stopped, Phase::Exchange)
                .with_cleanup(Cleanup::WorkloadStopUnconfirmed)
        ))
    );
}
