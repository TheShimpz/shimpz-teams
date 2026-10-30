//! The task's spend, stop, failure, and recovery bounds.

use super::*;

async fn spent(brain: Arc<FakeBrain>, spend: u64) -> (Outcome, u64) {
    let mut measured = task(
        &Driver::default(),
        brain,
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now() + MAX_SECONDS,
    )
    .await;
    measured.state.spend = spend;
    let outcome = measured.execute().await;
    (outcome, measured.state.spent)
}

#[tokio::test(start_paused = true)]
async fn spend_is_a_ceiling_of_worst_case_reservations_settled_by_reported_usage() {
    let reported = Usage {
        input_tokens: 10_000,
        output_tokens: 1_000,
        ..usage()
    };
    let mut steps = VecDeque::from([json(WAIT), json(DONE)]);
    let brain = FakeBrain::counting(reported, move |call, _| match call {
        BrowserCall::Step => (Ok(steps.pop_front().expect("a step")), Duration::ZERO),
        _ => (Ok(json(COMPLETE)), Duration::ZERO),
    });
    // Three calls (two steps and the check), each settled at the model's price.
    assert_eq!(
        spent(brain, MAX_SPEND).await.1,
        3 * (10_000 * 10 + 1_000 * 50)
    );
    // Attempts whose usage the Brain did not report keep their worst-case reservation.
    let unreported = Usage {
        provider_requests: 3,
        ..reported
    };
    let brain = FakeBrain::counting(unreported, |call, _| match call {
        BrowserCall::Step => (Ok(json(DONE)), Duration::ZERO),
        _ => (Ok(json(COMPLETE)), Duration::ZERO),
    });
    let floor = 2 * (10_000 * 10 + 1_000 * 50) + 2 * u64::from(MAX_CALL_OUTPUT_TOKENS) * 50;
    assert!(spent(brain, MAX_SPEND).await.1 > floor);
    // A step the normal share cannot reserve finishes the task instead of taking it.
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    assert_eq!(spent(brain, 1_000).await.0, Outcome::new(Status::Budget));
    // A failed call keeps its whole reservation.
    let brain = FakeBrain::steps(&[Err(StepFailed), Err(StepFailed)], &[], None);
    assert!(spent(brain, MAX_SPEND).await.1 > 0);
}

#[tokio::test(start_paused = true)]
async fn stop_budgets_and_failures_end_the_task_with_their_closed_reason() {
    let stop = Stop::new();
    stop.stop();
    let outcome = task(
        &Driver::default(),
        FakeBrain::steps(&[Ok(DONE)], &[], None),
        Arc::new(FakeHost::new()),
        stop,
        Instant::now() + MAX_SECONDS,
    )
    .await
    .run()
    .await;
    assert_eq!(outcome, Outcome::new(Status::Stopped));
    let blind = Driver::new(Browser {
        fail_reads: Some("tool-failed"),
        ..Browser::default()
    });
    let outcome = run(
        &blind,
        &FakeBrain::steps(&[Ok(DONE)], &[], None),
        FakeHost::new(),
    )
    .await;
    assert_eq!(
        outcome,
        failed(
            "browser-lost",
            0,
            Some("observe/tool-failed/reopen/driver-unavailable")
        )
    );
}

#[tokio::test(start_paused = true)]
async fn a_brain_that_cannot_answer_or_answers_outside_the_closed_set_ends_the_task() {
    let down = FakeBrain::steps(&[Err(StepFailed), Err(StepFailed)], &[], None);
    assert_eq!(
        run(&Driver::default(), &down, FakeHost::new()).await,
        failed("model-unavailable", 0, None)
    );
    // One failure is asked again: a model answers outside its form now and then.
    let once = FakeBrain::steps(&[Err(StepFailed), Ok(DONE)], &[], None);
    assert_eq!(
        run(&Driver::default(), &once, FakeHost::new()).await,
        done("Prazo: 30/11.", 1)
    );
    let invalid = FakeBrain::steps(&[Ok(r#"{"kind": "run_script"}"#)], &[], None);
    assert_eq!(
        run(&Driver::default(), &invalid, FakeHost::new()).await,
        failed("step-invalid", 1, None)
    );
    let fatal = Driver::new(Browser {
        fatal: vec!["click"],
        ..Browser::default()
    });
    let click = FakeBrain::steps(&[Ok(r#"{"kind": "click", "ref": "p1:1"}"#)], &[], None);
    assert_eq!(
        run(&fatal, &click, FakeHost::new()).await,
        failed(
            "browser-lost",
            1,
            Some("act/tool-failed/reopen/driver-unavailable")
        )
    );
}

#[tokio::test(start_paused = true)]
async fn every_call_and_action_waits_for_proof_that_the_task_may_continue() {
    let navigate = r#"{"kind": "navigate", "url": "https://example.com/a"}"#;
    // Stop arrives while the Brain answers: its step never reaches the page.
    for (answer, steps) in [(Ok(navigate), 1), (Err(StepFailed), 0)] {
        let stop = Stop::new();
        let stopping = stop.clone();
        let answer = answer.map(json);
        let brain = FakeBrain::new(move |_, _| {
            stopping.stop();
            (answer.clone(), Duration::ZERO)
        });
        let driver = Driver::default();
        let outcome = task(
            &driver,
            brain,
            Arc::new(FakeHost::new()),
            stop,
            Instant::now() + MAX_SECONDS,
        )
        .await
        .run()
        .await;
        // Stop aborts the Brain request in flight: the task was stopped, not failed.
        assert_eq!(outcome, Outcome::new(Status::Stopped).steps(steps));
        assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
    }
    // The Browser left the egress process it started on, before or while the Brain answered.
    let brain = FakeBrain::steps(&[Ok(navigate)], &[], None);
    let outcome = run(&Driver::default(), &brain, FakeHost::new().intact(|| false)).await;
    assert_eq!(
        outcome,
        failed("browser-lost", 0, Some("runtime/not-intact"))
    );
    let lost = Arc::new(Mutex::new(false));
    let losing = Arc::clone(&lost);
    let brain = FakeBrain::new(move |_, _| {
        *losing.lock().expect("lost") = true;
        (Ok(json(navigate)), Duration::ZERO)
    });
    let driver = Driver::default();
    let outcome = run(
        &driver,
        &brain,
        FakeHost::new().intact(move || !*lost.lock().expect("lost")),
    )
    .await;
    assert_eq!(
        outcome,
        failed("browser-lost", 1, Some("runtime/not-intact"))
    );
    assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
}

#[tokio::test(start_paused = true)]
async fn a_failed_driver_session_is_replaced_and_the_task_continues_where_it_was() {
    let failed_driver = Driver::new(Browser {
        fatal: vec!["click"],
        ..Browser::default()
    });
    let fresh = Driver::default();
    let replacement = fresh.clone();
    let host =
        Arc::new(FakeHost::new().reopening(move |relaunch| Ok((replacement.clone(), relaunch))));
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "click", "ref": "p1:1"}"#),
            Ok(r#"{"kind": "scroll", "direction": "down"}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    let deadline = Instant::now() + MAX_SECONDS;
    let outcome = task(
        &failed_driver,
        Arc::clone(&brain),
        Arc::clone(&host),
        Stop::new(),
        deadline,
    )
    .await
    .run()
    .await;
    assert_eq!(
        outcome,
        Outcome {
            recovered: 1,
            ..done("Prazo: 30/11.", 3)
        }
    );
    assert_eq!(host.reopened(), [(deadline, false)]);
    // The failed session is never used again, and the click whose effect is unknown is in the thread as such.
    assert_eq!(failed_driver.calls(), [call(&["click", "p1:1"])]);
    assert_eq!(fresh.calls(), [call(&["scroll", "down"])]);
    assert_eq!(outcomes(&last_steps(&brain)), ["interrupted", "ok"]);
}

#[tokio::test(start_paused = true)]
async fn an_observation_that_failed_is_looked_at_again_on_a_fresh_session() {
    let fresh = Driver::default();
    let replacement = fresh.clone();
    let blind = Driver::new(Browser {
        fail_reads: Some("tool-failed"),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let host = FakeHost::new().reopening(move |relaunch| Ok((replacement.clone(), relaunch)));
    let outcome = run(&blind, &brain, host).await;
    assert_eq!(
        outcome,
        Outcome {
            recovered: 1,
            ..done("Prazo: 30/11.", 2)
        }
    );
    assert_eq!(
        brain.requests(BrowserCall::Step)[0]
            .get("thread")
            .and_then(|thread| thread.get("steps")),
        Some(&json("[]"))
    );
    assert_eq!(fresh.browser().screenshots.len(), 1);
}

#[tokio::test(start_paused = true)]
async fn a_page_chromium_no_longer_reads_starts_chromium_again_and_the_agent_is_told() {
    let refusing = || {
        Driver::new(Browser {
            fail_reads: Some("browser-read-refused"),
            ..Browser::default()
        })
    };
    let relaunches = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::clone(&relaunches);
    let host = FakeHost::new().reopening(move |relaunch| {
        seen.lock().expect("seen").push(relaunch);
        Ok((Driver::default(), relaunch))
    });
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let outcome = run(&refusing(), &brain, host).await;
    assert_eq!(
        outcome,
        Outcome {
            recovered: 1,
            relaunched: 1,
            ..done("Prazo: 30/11.", 2)
        }
    );
    assert_eq!(*relaunches.lock().expect("seen"), [true]);
    // Only the next observation says so; the one after it does not.
    assert!(observation_of(&brain.requests(BrowserCall::Step)[0]).starts_with(RESTARTED_NOTICE));
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "scroll", "direction": "down"}"#), Ok(DONE)],
        &[],
        None,
    );
    let host = FakeHost::new().reopening(|relaunch| Ok((Driver::default(), relaunch)));
    run(&refusing(), &brain, host).await;
    assert!(!observation_of(&brain.requests(BrowserCall::Step)[1]).starts_with(RESTARTED_NOTICE));
}

#[tokio::test(start_paused = true)]
async fn a_session_replaced_on_a_chromium_started_again_is_told_and_counted() {
    // Any failure may need Chromium started again: the replacement says whether it did.
    let blind = Driver::new(Browser {
        fail_reads: Some("tool-failed"),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let host = FakeHost::new().reopening(|_| Ok((Driver::default(), true)));
    let outcome = run(&blind, &brain, host).await;
    assert_eq!(outcome.relaunched, 1);
    assert!(observation_of(&brain.requests(BrowserCall::Step)[0]).starts_with(RESTARTED_NOTICE));
}

#[tokio::test(start_paused = true)]
async fn recoveries_are_bounded_and_a_failed_one_ends_the_task_with_its_cause() {
    let blind = || {
        Driver::new(Browser {
            fail_reads: Some("tool-failed"),
            ..Browser::default()
        })
    };
    let host = FakeHost::new().reopening(move |relaunch| Ok((blind(), relaunch)));
    let outcome = run(&blind(), &FakeBrain::steps(&[Ok(DONE)], &[], None), host).await;
    let steps = usize::try_from(MAX_RECOVERIES).expect("small");
    assert_eq!(
        outcome,
        Outcome {
            recovered: MAX_RECOVERIES,
            ..failed("browser-lost", steps, Some("observe/tool-failed"))
        }
    );
    let host = FakeHost::new().reopening(|_| Err(CuaError::fatal("driver-not-ended")));
    let outcome = run(&blind(), &FakeBrain::steps(&[], &[], None), host).await;
    assert_eq!(
        outcome,
        failed(
            "browser-lost",
            0,
            Some("observe/tool-failed/reopen/driver-not-ended")
        )
    );
}

#[tokio::test(start_paused = true)]
async fn stop_and_a_lost_egress_win_over_a_recovery() {
    let blind = || {
        Driver::new(Browser {
            fail_reads: Some("tool-failed"),
            ..Browser::default()
        })
    };
    // Stop came while the session was being replaced.
    let stop = Stop::new();
    let stopping = stop.clone();
    let host = Arc::new(FakeHost::new().reopening(move |_| {
        stopping.stop();
        Err(CuaError::fatal("driver-unavailable"))
    }));
    let outcome = task(
        &blind(),
        FakeBrain::steps(&[], &[], None),
        Arc::clone(&host),
        stop,
        Instant::now() + MAX_SECONDS,
    )
    .await
    .run()
    .await;
    assert_eq!(outcome, Outcome::new(Status::Stopped));
    assert_eq!(host.reopened().len(), 1);
    // The egress was lost before the session failed: no session is replaced.
    let mut answers = VecDeque::from([true, false]);
    let host = Arc::new(FakeHost::new().intact(move || answers.pop_front().unwrap_or(false)));
    let outcome = task(
        &blind(),
        FakeBrain::steps(&[], &[], None),
        Arc::clone(&host),
        Stop::new(),
        Instant::now() + MAX_SECONDS,
    )
    .await
    .run()
    .await;
    assert_eq!(
        outcome,
        failed("browser-lost", 0, Some("runtime/not-intact"))
    );
    assert_eq!(host.reopened(), []);
}

#[tokio::test(start_paused = true)]
async fn no_call_outlives_the_deadline_the_task_was_given() {
    let deadline = Instant::now() + Duration::from_secs(500);
    let brain = FakeBrain::steps(&[Ok(WAIT), Ok(DONE)], &[], None);
    let mut given = task(
        &Driver::default(),
        Arc::clone(&brain),
        Arc::new(FakeHost::new()),
        Stop::new(),
        deadline,
    )
    .await;
    given.execute().await;
    assert_eq!(
        given.session.as_ref().and_then(CuaSession::deadline),
        Some(deadline)
    );
    let calls = brain.calls();
    assert!(!calls.is_empty());
    assert!(calls.iter().all(|(_, _, until)| *until <= deadline));
}
