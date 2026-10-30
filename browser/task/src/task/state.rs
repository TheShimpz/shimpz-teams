//! The task's running state: its budget, its Brain calls and their records, and the check of its answer.

use super::{
    BrowserCall, FINISH_SECONDS, FINISH_SHARE, Instant, MAX_CALL_ATTEMPTS, MAX_CALL_OUTPUT_TOKENS,
    MAX_NOTE_CHARS, Observation, Outcome, PROMPT_TOKENS, State, Status, StepFailed, Usage, Value,
    Verdict, bound, canonical_check, canonical_memory, count, entry_bytes, object, text, with,
};

impl State {
    pub(super) fn counted(&self, outcome: Outcome) -> Outcome {
        Outcome {
            recovered: self.recovered,
            relaunched: self.relaunched,
            ..outcome
        }
    }

    /// Why the task makes no further call or action: Stop, no time left, or a Browser no longer intact.
    pub(super) async fn halted(&self, steps: usize) -> Option<Outcome> {
        if self.stop.is_stopped() {
            return Some(Outcome::new(Status::Stopped).steps(steps));
        }
        if Instant::now() >= self.deadline {
            return Some(Outcome::new(Status::Budget).steps(steps));
        }
        if !self.host.intact().await {
            return Some(Outcome::failed(
                "browser-lost",
                steps,
                Some("runtime/not-intact".to_owned()),
            ));
        }
        None
    }

    pub(super) fn finishing(&self) -> bool {
        Instant::now() + FINISH_SECONDS >= self.deadline
    }

    pub(super) fn affords(&self, reserve: u64, finishing: bool) -> bool {
        let share = if finishing { 100 } else { 100 - FINISH_SHARE };
        self.spent.saturating_add(reserve) <= self.spend * share / 100
    }

    /// One provider attempt's worst cost: its input bound and the most output a call may write.
    pub(super) fn attempt(&self, bound: u64) -> u64 {
        let (price_in, price_out) = (self.price)(self.model.as_deref());
        bound
            .saturating_mul(price_in)
            .saturating_add(u64::from(MAX_CALL_OUTPUT_TOKENS).saturating_mul(price_out))
    }

    /// One Brain call under the spend ceiling; `input` is the call's input bound when its request is no step request.
    pub(super) async fn ask(
        &mut self,
        call: BrowserCall,
        request: &Value,
        finishing: bool,
        input: Option<u64>,
    ) -> Result<Value, StepFailed> {
        let attempt = self.attempt(input.unwrap_or_else(|| bound(request)));
        let reserve = attempt.saturating_mul(u64::from(MAX_CALL_ATTEMPTS));
        if !self.affords(reserve, finishing) || self.halted(0).await.is_some() {
            return Err(StepFailed);
        }
        let until = if finishing {
            self.deadline
        } else {
            self.deadline
                .checked_sub(FINISH_SECONDS)
                .unwrap_or(self.deadline)
        };
        if until <= Instant::now() {
            return Err(StepFailed);
        }
        match self.brain.call(call, request.clone(), until).await {
            Err(StepFailed) => {
                self.spent = self.spent.saturating_add(reserve);
                Err(StepFailed)
            }
            Ok(answered) => {
                self.model = Some(answered.model);
                self.spent = self
                    .spent
                    .saturating_add(self.settled(&answered.usage, attempt));
                Ok(answered.value)
            }
        }
    }

    /// A call's reported usage at its model's price, and every attempt that reported none at its worst case.
    pub(super) fn settled(&self, usage: &Usage, attempt: u64) -> u64 {
        let (price_in, price_out) = (self.price)(self.model.as_deref());
        let reported = usage
            .model_calls
            .saturating_sub(usage.failed_calls)
            .saturating_sub(usage.unreported_calls);
        let unaccounted = usage.provider_requests.saturating_sub(reported);
        usage
            .input_tokens
            .saturating_mul(price_in)
            .saturating_add(usage.output_tokens.saturating_mul(price_out))
            .saturating_add(unaccounted.saturating_mul(attempt))
    }

    pub(super) fn request(&self, observation: &Observation, downloads: Option<&Value>) -> Value {
        let screenshot = observation
            .screenshot
            .as_deref()
            .map_or(Value::Null, |image| text(&crate::cua::base64(image)));
        object(vec![
            ("objective", text(&self.objective)),
            ("language", text(&self.language)),
            ("thread", self.thread.wire()),
            ("observation", text(&observation.text)),
            ("screenshot", screenshot),
            ("downloads", downloads.cloned().unwrap_or(Value::Null)),
        ])
    }

    /// Appends a step as its outcome arrives, folding the oldest steps first when it would not fit.
    pub(super) async fn record(
        &mut self,
        request: &Value,
        step: Value,
        outcome: &str,
        observation: &Observation,
        downloads: Option<&Value>,
    ) {
        let entry = object(vec![
            ("step", step),
            ("outcome", text(outcome)),
            (
                "page",
                object(vec![
                    ("url", text(&observation.url)),
                    ("title", text(&observation.title)),
                ]),
            ),
            ("text", text(&observation.text)),
            ("downloads", downloads.cloned().unwrap_or(Value::Null)),
            ("screenshot", Value::Bool(observation.screenshot.is_some())),
        ]);
        let folding = self.thread.to_fold(entry_bytes(&entry));
        if folding > 0 {
            let (first, last) = (self.thread.number(0), self.thread.number(folding - 1));
            let memory = self.memory(request, first, last).await;
            self.thread.fold(folding, memory);
        }
        self.thread.append(entry);
    }

    /// The agent's whole rewritten memory as steps leave its view; `None` when it could not be written.
    pub(super) async fn memory(
        &mut self,
        request: &Value,
        first: u64,
        last: u64,
    ) -> Option<String> {
        let request = with(
            request,
            vec![("first", count(first)), ("last", count(last))],
        );
        let value = self
            .ask(BrowserCall::Fold, &request, false, None)
            .await
            .ok()?;
        match &value {
            Value::Object(members) if members.len() == 1 => {
                canonical_memory(value.get("memory")?).map(str::to_owned)
            }
            _ => None,
        }
    }

    /// Gives a done answer back to the agent: a `returned` record whose note says what it still needs.
    pub(super) async fn give_back(
        &mut self,
        request: &Value,
        observation: &Observation,
        downloads: Option<&Value>,
        note: &str,
    ) {
        let note: String = note.chars().take(MAX_NOTE_CHARS).collect();
        let step = object(vec![("kind", text("wait")), ("note", text(&note))]);
        self.record(request, step, "returned", observation, downloads)
            .await;
    }

    /// The completeness check's verdict, or not checked when it could not run or answered outside its form.
    pub(super) async fn check(&mut self, summary: &str) -> Verdict {
        let request = object(vec![
            ("objective", text(&self.objective)),
            ("language", text(&self.language)),
            ("summary", text(summary)),
        ]);
        let bound =
            (self.objective.len() + self.language.len() + summary.len()) as u64 + PROMPT_TOKENS;
        let Ok(value) = self
            .ask(BrowserCall::Check, &request, true, Some(bound))
            .await
        else {
            return Verdict::default();
        };
        let Some(verdict) = canonical_check(&value) else {
            return Verdict::default();
        };
        Verdict {
            complete: match verdict.get("complete") {
                Some(Value::Bool(complete)) => Some(*complete),
                _ => None,
            },
            missing: match verdict.get("missing") {
                Some(Value::String(missing)) => Some(missing.clone()),
                _ => None,
            },
        }
    }
}
