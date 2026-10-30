//! The task's thread: its folds, its memory, and the answers it goes back on.

use super::*;

fn long_pages() -> Driver {
    let mut browser = Browser::default();
    browser.views.insert(
        "https://example.com".to_owned(),
        View {
            url: "https://example.com".to_owned(),
            title: "Long".to_owned(),
            outline: "x".repeat(MAX_OBSERVATION_BYTES),
            elements: Vec::<Element>::new(),
        },
    );
    Driver::new(browser)
}

#[tokio::test(start_paused = true)]
async fn a_thread_past_its_bound_folds_its_oldest_steps_and_keeps_the_rewritten_memory() {
    let mut memories: VecDeque<&str> = std::iter::once("V101: R$ 9.500")
        .chain(std::iter::repeat_n("V101: R$ 9.500\nV102: R$ 7.000", 10))
        .collect();
    let mut steps = 0;
    let brain = FakeBrain::new(move |call, _| {
        let answer = match call {
            BrowserCall::Fold => Value::Object(vec![(
                "memory".to_owned(),
                Value::String(memories.pop_front().expect("a memory").to_owned()),
            )]),
            BrowserCall::Check => json(COMPLETE),
            _ => {
                steps += 1;
                json(if steps <= 12 { WAIT } else { DONE })
            }
        };
        (Ok(answer), Duration::ZERO)
    });
    let outcome = run(&long_pages(), &brain, FakeHost::new()).await;
    assert_eq!(outcome.status, Status::Done);
    let folds = brain.requests(BrowserCall::Fold);
    assert!(folds.len() >= 2);
    assert_eq!(
        folds[1]
            .get("thread")
            .and_then(|thread| thread.get("memory")),
        Some(&json(r#""V101: R$ 9.500""#))
    );
    let requests = brain.requests(BrowserCall::Step);
    let thread = requests
        .last()
        .and_then(|request| request.get("thread"))
        .expect("a thread");
    assert_eq!(
        thread.get("memory"),
        Some(&json(r#""V101: R$ 9.500\nV102: R$ 7.000""#))
    );
    assert_eq!(canonical_thread(thread).as_ref(), Some(thread));
    for request in &requests {
        assert!(history_bytes(request.get("thread").expect("a thread")) <= MAX_HISTORY_BYTES);
    }
}

#[tokio::test(start_paused = true)]
async fn a_fold_whose_memory_could_not_be_written_counts_its_steps_as_unnoted() {
    let mut steps = 0;
    let brain = FakeBrain::new(move |call, _| match call {
        BrowserCall::Fold => (Err(StepFailed), Duration::ZERO),
        BrowserCall::Check => (Ok(json(COMPLETE)), Duration::ZERO),
        _ => {
            steps += 1;
            (
                Ok(json(if steps <= 12 { WAIT } else { DONE })),
                Duration::ZERO,
            )
        }
    });
    run(&long_pages(), &brain, FakeHost::new()).await;
    let number = |request: &Value, name: &str| match request.get(name) {
        Some(Value::Number(strict_json::Number::Int(number))) => *number,
        _ => 0,
    };
    let folded: i64 = brain
        .requests(BrowserCall::Fold)
        .iter()
        .map(|fold| number(fold, "last") - number(fold, "first") + 1)
        .sum();
    let requests = brain.requests(BrowserCall::Step);
    let last = requests
        .last()
        .and_then(|request| request.get("thread"))
        .expect("a thread");
    assert!(folded > 0);
    assert_eq!(
        (last.get("memory"), number(last, "unnoted")),
        (Some(&json(r#""""#)), folded)
    );
}

#[tokio::test(start_paused = true)]
async fn an_answer_citing_an_unshown_url_goes_back_twice_then_says_so() {
    let cited = r#"{"kind": "done", "summary": "Prazo: 30/11 (https://example.com/edital)."}"#;
    let brain = FakeBrain::steps(&[Ok(cited), Ok(cited), Ok(cited)], &[], None);
    let outcome = run(&Driver::default(), &brain, FakeHost::new()).await;
    assert_eq!(
        outcome,
        Outcome {
            unscreened: vec!["https://example.com/edital".to_owned()],
            ..done("Prazo: 30/11 (https://example.com/edital).", 3)
        }
    );
    let returned = last_steps(&brain);
    assert_eq!(outcomes(&returned), ["returned", "returned"]);
    let note = returned[0].get("step").and_then(|step| step.get("note"));
    assert!(matches!(note, Some(Value::String(note)) if note.starts_with(UNSCREENED_NOTE)));
    // A cited page the task observed is screened.
    let brain = FakeBrain::steps(
        &[Ok(
            r#"{"kind": "done", "summary": "Fonte: https://example.com."}"#,
        )],
        &[],
        None,
    );
    assert_eq!(
        run(&Driver::default(), &brain, FakeHost::new())
            .await
            .unscreened,
        Vec::<String>::new()
    );
}

#[tokio::test(start_paused = true)]
async fn an_incomplete_answer_goes_back_four_times_then_travels_with_what_is_missing() {
    let missing = r#"{"complete": false, "missing": "Faltam 3 vagas."}"#;
    let brain = FakeBrain::steps(&[Ok(DONE); 5], &[Ok(missing); 5], None);
    let outcome = run(&Driver::default(), &brain, FakeHost::new()).await;
    let expected = Outcome {
        completeness: Some("incomplete"),
        missing: Some("Faltam 3 vagas.".to_owned()),
        ..done("Prazo: 30/11.", 5)
    };
    assert_eq!(outcome, expected);
    let steps = last_steps(&brain);
    let notes: Vec<Option<&Value>> = steps
        .iter()
        .map(|entry| entry.get("step").and_then(|step| step.get("note")))
        .collect();
    assert_eq!(notes.len(), 4);
    assert!(notes.iter().all(
        |note| matches!(note, Some(Value::String(note)) if note.starts_with(INCOMPLETE_NOTE))
    ));
    // A check that failed or answered outside its form is reported as not checked, never as complete.
    for verdict in [
        Err(StepFailed),
        Ok(r#"{"complete": "yes"}"#),
        Ok(r#"{"complete": null, "missing": null}"#),
    ] {
        let brain = FakeBrain::steps(&[Ok(DONE)], &[verdict], None);
        let outcome = run(&Driver::default(), &brain, FakeHost::new()).await;
        assert_eq!(outcome.completeness, Some("not-checked"));
    }
}

#[tokio::test(start_paused = true)]
async fn past_the_soft_cutoff_the_brain_answers_from_the_thread_and_never_takes_another_step() {
    let last = r#"{"summary": "Encontrei 7 de 10 vagas."}"#;
    let brain = FakeBrain::steps(&[Ok(WAIT)], &[], Some(last));
    let outcome = task(
        &Driver::default(),
        Arc::clone(&brain),
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now() + Duration::from_secs(30),
    )
    .await
    .run()
    .await;
    let expected = Outcome {
        completeness: Some("not-checked"),
        ..done("Encontrei 7 de 10 vagas.", 0)
    };
    assert_eq!(outcome, expected);
    assert_eq!(brain.operations(), [BrowserCall::Final]);
    // The steps run out the same way, and a final answer that could not be written ends with the budget.
    for (last, expected) in [(Some(last), Some("Encontrei 7 de 10 vagas.")), (None, None)] {
        let brain = FakeBrain::steps(&[Ok(WAIT)], &[], last);
        let mut one = task(
            &Driver::default(),
            brain,
            Arc::new(FakeHost::new()),
            Stop::new(),
            Instant::now() + MAX_SECONDS,
        )
        .await;
        one.state.steps = 1;
        let outcome = one.execute().await;
        assert_eq!(outcome.summary.as_deref(), expected);
        if expected.is_none() {
            assert_eq!(outcome, Outcome::new(Status::Budget).steps(1));
        }
    }
}
