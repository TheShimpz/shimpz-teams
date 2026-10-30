use std::time::Duration;

use strict_json::Value;
use team_core::exec::ExecExit;
use team_core::stop::Stop;
use team_core::testing::{FakeExec, ScriptedExec};
use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};
use tokio::time::Instant;

use super::{
    DockerSlots, OPERATION_ID_PLACEHOLDER, RpcCall, RpcError, RpcFailure, encode_invocation,
    exchange, terminal,
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

#[tokio::test]
async fn transport_faults_fail_stop_and_record_only_the_safe_condition() {
    let cases = [
        (frame(1, b"{}"), Some(1), "exit-status:1"),
        (
            [frame(1, b"{}"), frame(2, b"Traceback: secret")].concat(),
            Some(0),
            "stderr-output",
        ),
        (frame(1, b"{}"), None, "exit-unavailable"),
        (b"garbage!".to_vec(), Some(0), "frame-invalid"),
    ];
    for (output, code, expected) in cases {
        let engine = FakeExec::default();
        scripted(&engine, output, code);
        let Err(RpcError::Failed { condition, .. }) =
            run(&engine, &DockerSlots::default(), &Stop::new(), soon()).await
        else {
            panic!("a failure");
        };
        assert_eq!((condition.as_str(), engine.stopped().len()), (expected, 1));
    }
}

#[tokio::test]
async fn exec_fail_stop_proves_not_running_or_blocks() {
    let engine = FakeExec::default();
    engine.block_fail_stop();
    scripted(&engine, frame(1, b"{}"), Some(3));
    assert_eq!(
        run(&engine, &DockerSlots::default(), &Stop::new(), soon()).await,
        Err(RpcError::Blocked)
    );
}

#[tokio::test]
async fn a_stopped_turn_or_saturated_pool_never_dispatches() {
    let engine = FakeExec::default();
    let stop = Stop::new();
    stop.stop();
    assert_eq!(
        run(&engine, &DockerSlots::default(), &stop, soon()).await,
        Err(RpcError::NotDispatched)
    );
    let saturated = DockerSlots::with(0);
    let deadline = Instant::now() + Duration::from_millis(50);
    assert_eq!(
        run(&engine, &saturated, &Stop::new(), deadline).await,
        Err(RpcError::NotDispatched)
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
    let Err(RpcError::Failed { kind, condition }) =
        run(&engine, &DockerSlots::default(), &Stop::new(), deadline).await
    else {
        panic!("a timeout");
    };
    drop(workload);
    assert_eq!(
        (kind, condition.as_str(), engine.stopped().len()),
        (RpcFailure::Timeout, "timeout", 1)
    );
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
    assert_eq!(
        refused,
        Err(RpcError::Failed {
            kind: RpcFailure::InvalidResult,
            condition: "request-after-provider-call".to_owned()
        })
    );
    assert!(terminal(br#"{"type":"result","result":{}}"#, 1).is_ok());
    for invalid in [b"{".as_slice(), b"[]"] {
        assert_eq!(
            terminal(invalid, 0),
            Err(RpcError::Failed {
                kind: RpcFailure::InvalidResult,
                condition: "frame-invalid".to_owned()
            })
        );
    }
}
