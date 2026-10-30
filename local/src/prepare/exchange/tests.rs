use std::time::Duration;

use strict_json::Value;
use team_core::exec::ExecExit;
use team_core::testing::{FakeExec, ScriptedExec};
use team_prepare::limits::MAX_HELPER_OUTPUT_BYTES;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};

use super::{ExchangeFailed, exchange, spec};

const EXITED: ExecExit = ExecExit {
    running: false,
    exit_code: Some(0),
};

fn frame(stream: u8, payload: &[u8]) -> Vec<u8> {
    let mut framed = vec![stream, 0, 0, 0];
    framed.extend_from_slice(&u32::try_from(payload.len()).expect("small").to_be_bytes());
    framed.extend_from_slice(payload);
    framed
}

/// Scripts one exec whose workload reads the whole request, then writes `output` and closes.
fn scripted(
    engine: &FakeExec,
    exit: ExecExit,
    output: Vec<u8>,
) -> tokio::task::JoinHandle<Vec<u8>> {
    let (team_end, workload_end) = duplex(64 * 1024);
    engine.push(ScriptedExec {
        stream: team_end,
        exit,
    });
    tokio::spawn(workload(workload_end, output))
}

async fn workload(mut stream: DuplexStream, output: Vec<u8>) -> Vec<u8> {
    let mut request = Vec::new();
    stream
        .read_to_end(&mut request)
        .await
        .expect("the request ends with a half-close");
    let _ = stream.write_all(&output).await;
    request
}

#[tokio::test]
async fn one_answer_is_read_after_the_request_is_delivered_with_its_end() {
    let engine = FakeExec::default();
    let workload = scripted(
        &engine,
        EXITED,
        [
            frame(1, br#"{"type":"text","#),
            frame(1, br#""text":"ok"}"#),
        ]
        .concat(),
    );
    let answer = exchange(&engine, "helper", b"{\"kind\":\"pdf\",\"size\":1}\nx").await;
    assert_eq!(
        answer.as_ref().ok().and_then(|value| value.get("text")),
        Some(&Value::String("ok".to_owned()))
    );
    assert_eq!(
        workload.await.expect("ran"),
        b"{\"kind\":\"pdf\",\"size\":1}\nx"
    );
    assert_eq!(engine.started(), [spec("helper")]);
    let started = &engine.started()[0];
    assert_eq!(started.argv, ["/usr/local/bin/shimpz-team", "prepare"]);
    assert_eq!(
        (started.user.as_str(), started.env.len(), started.stdin),
        ("65534:65534", 0, true)
    );
}

#[tokio::test]
async fn standard_error_another_stream_or_a_malformed_frame_fails_the_file() {
    for output in [
        [frame(2, b"warning"), frame(1, b"{}")].concat(),
        [frame(1, b"{}"), frame(2, b"late")].concat(),
        frame(0, b"{}"),
        frame(3, b"{}"),
        [
            vec![1, 0, 1, 0],
            2_u32.to_be_bytes().to_vec(),
            b"{}".to_vec(),
        ]
        .concat(),
        frame(1, b"{")[..6].to_vec(),
        frame(1, b"not json"),
        frame(1, b"{}")[..9].to_vec(),
    ] {
        let engine = FakeExec::default();
        drop(scripted(&engine, EXITED, output.clone()));
        assert_eq!(
            exchange(&engine, "helper", b"x").await,
            Err(ExchangeFailed),
            "{output:?}"
        );
    }
}

#[tokio::test]
async fn a_frame_over_the_output_budget_is_refused_before_its_payload_is_read() {
    let engine = FakeExec::default();
    let announced = u32::try_from(MAX_HELPER_OUTPUT_BYTES + 1).expect("fits");
    let header = [vec![1, 0, 0, 0], announced.to_be_bytes().to_vec()].concat();
    // The payload never comes: only the header's announcement can refuse it.
    let (team_end, mut workload_end) = duplex(64);
    engine.push(ScriptedExec {
        stream: team_end,
        exit: EXITED,
    });
    let workload = tokio::spawn(async move {
        let mut request = Vec::new();
        workload_end.read_to_end(&mut request).await.expect("read");
        workload_end.write_all(&header).await.expect("written");
        tokio::time::sleep(Duration::from_secs(3600)).await;
        drop(workload_end);
    });
    assert_eq!(exchange(&engine, "helper", b"x").await, Err(ExchangeFailed));
    workload.abort();
    // Frames that together exceed the budget are refused too.
    let engine = FakeExec::default();
    let half = vec![b' '; MAX_HELPER_OUTPUT_BYTES / 2 + 1];
    drop(scripted(
        &engine,
        EXITED,
        [frame(1, &half), frame(1, &half)].concat(),
    ));
    assert_eq!(exchange(&engine, "helper", b"x").await, Err(ExchangeFailed));
}

#[tokio::test]
async fn a_non_zero_exit_or_a_running_process_fails_the_file() {
    for exit in [
        ExecExit {
            running: false,
            exit_code: Some(1),
        },
        ExecExit {
            running: true,
            exit_code: None,
        },
        ExecExit {
            running: false,
            exit_code: None,
        },
    ] {
        let engine = FakeExec::default();
        drop(scripted(&engine, exit, frame(1, b"{}")));
        assert_eq!(
            exchange(&engine, "helper", b"x").await,
            Err(ExchangeFailed),
            "{exit:?}"
        );
    }
    // An exec that cannot start fails the file too.
    assert_eq!(
        exchange(&FakeExec::default(), "helper", b"x").await,
        Err(ExchangeFailed)
    );
}

#[tokio::test(start_paused = true)]
async fn a_worker_that_never_answers_fails_at_the_wall_time() {
    let engine = FakeExec::default();
    let (team_end, mut workload_end) = duplex(64);
    engine.push(ScriptedExec {
        stream: team_end,
        exit: EXITED,
    });
    let silent = tokio::spawn(async move {
        let mut request = Vec::new();
        let _ = workload_end.read_to_end(&mut request).await;
        tokio::time::sleep(Duration::from_secs(3600)).await;
        drop(workload_end);
    });
    let started = tokio::time::Instant::now();
    assert_eq!(exchange(&engine, "helper", b"x").await, Err(ExchangeFailed));
    assert_eq!(started.elapsed(), Duration::from_secs(10));
    silent.abort();
}
