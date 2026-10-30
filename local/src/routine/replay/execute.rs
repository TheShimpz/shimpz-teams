//! One registered segment of a replayed run, and exactly how it ended (`replay.execute`, `replay._ended`): complete,
//! frozen for a person, stopped with nothing uncertain, failed with nothing dispatched or an input it could not resolve
//! after only read-only steps, and otherwise held as an incident for recovery.

use std::sync::Arc;
use std::time::Instant;

use strict_json::Value;
use team_chat::orchestrator::Outcome;
use team_chat::progress::Reporter;
use team_protocol::python::value::object_of;
use team_routine::record::Run;

use super::segment::Segment;
use super::{Progress, ReplayRuntime};
use crate::chat::pending::PendingLocalChat;
use crate::http::service::Problem;
use crate::routine::executor::DispatchHooks;
use crate::routine::run::LeasedRun;
use crate::routine::service::RoutineService;

/// Team's own resolution of a step's input from earlier results, refused before that step can dispatch (ADR-0101).
pub const UNRESOLVED_INPUTS: [&str; 3] = [
    "plan-reference-missing",
    "plan-reference-ambiguous",
    "plan-reference-invalid",
];

/// The run's own segment: in its generation, against its Assistants, with one Action round per step of its plan.
fn segment<'a>(
    run: &'a LeasedRun<'_>,
    value: &'a Run,
    scope: (&'a [String], &'a Reporter),
    pending: Option<&PendingLocalChat>,
    max_rounds: usize,
) -> Segment<'a> {
    let (assistant_ids, progress) = scope;
    Segment {
        team_id: &run.team_id,
        run_id: &run.run_id,
        generation: &value.generation,
        assistant_ids,
        stop: run.stop(),
        progress,
        locale: pending.and_then(|pending| pending.locale.clone()),
        continuation: pending
            .map(|pending| (pending.continuation.clone(), pending.identity.clone())),
        max_rounds,
        held: true,
    }
}

impl RoutineService {
    /// Runs one registered segment of a replayed run in the held execution slot and records exactly how it ended.
    ///
    /// # Errors
    /// A state problem the ending could not record.
    pub(crate) async fn execute(
        &self,
        run: &LeasedRun<'_>,
        value: &Run,
        progress: &Reporter,
        pending: Option<&PendingLocalChat>,
    ) -> Result<&'static str, Problem> {
        let started = Instant::now();
        let held = (run.stop().clone(), Arc::clone(&run.protected));
        let runtime = match self.runtime(&run.team_id, value, &run.routine, held).await {
            Ok(runtime) => runtime,
            Err(code) => {
                // A failed segment's active time is charged under its live lease before it is fenced.
                self.spend_time(&run.team_id, &run.run_id, &run.lease, started.elapsed());
                return self.ended(run, value, None, code);
            }
        };
        for transcript in &run.transcripts {
            let transcripts = &self.parts.chat.parts().transcripts;
            transcripts.set(&run.team_id, transcript.clone());
        }
        let ids: Vec<String> = run
            .routine
            .assistants
            .iter()
            .map(|(id, _)| id.clone())
            .collect();
        let segment = segment(
            run,
            value,
            (&ids, progress),
            pending,
            runtime.plan().steps.len(),
        );
        let hooks: Arc<dyn DispatchHooks> = runtime.clone();
        let result = self.run_segment(&segment, runtime.as_ref(), hooks).await;
        self.spend_time(&run.team_id, &run.run_id, &run.lease, started.elapsed());
        let finished = match result {
            Ok(finished) => finished,
            Err(cut) => {
                let code = runtime.failure().unwrap_or(cut.code);
                return self.ended(run, value, Some((&runtime, cut.held)), code);
            }
        };
        if let Outcome::Completed(_) = finished.outcome {
            let shown = runtime.cursor().shown;
            return self.finish_run(run, value, shown.as_ref());
        }
        // The run waits at its current step, which its step record names by position.
        runtime.record_step("waiting", None);
        self.suspended(run, &finished, runtime.cursor().step + 1)
    }

    /// How a segment that raised ends; whether anything was dispatched is read from the run's sealed cursor,
    /// snapshot, and journal, never from a runtime that may have failed to open them.
    fn ended(
        &self,
        run: &LeasedRun<'_>,
        value: &Run,
        segment: Option<(&ReplayRuntime, bool)>,
        code: &'static str,
    ) -> Result<&'static str, Problem> {
        let uncertain = self.uncertain(value, segment.is_some_and(|(_, held)| held));
        let mut code = code;
        if code == "chat-stopped" {
            if let Some((runtime, _)) = segment {
                // An attempt still in flight when Stop or the deadline cut the segment is recorded as stopped.
                runtime.record_step("stopped", None);
            }
            if self.overdue(&run.run_id) {
                code = "active-time-exceeded";
                if self.progress(&run.team_id, value) == Progress::Done {
                    // Out of time, not stopped by a person, after its sealed cursor completed every step.
                    let shown = self.sealed_shown(&run.team_id, value);
                    return self.complete_sealed(run, shown.as_ref());
                }
            } else if !uncertain {
                return self.end_run(&run.team_id, &run.run_id, "stopped", object_of(Vec::new()));
            }
        }
        let unresolved =
            UNRESOLVED_INPUTS.contains(&code) && self.read_only_prefix(&run.team_id, value);
        if !uncertain && (unresolved || self.progress(&run.team_id, value) == Progress::None) {
            let detail = object_of(vec![("code", Value::String(code.to_owned()))]);
            return self.end_run(&run.team_id, &run.run_id, "failed", detail);
        }
        self.hold(&run.team_id, &run.run_id, &run.lease)?;
        Ok("held")
    }
}
