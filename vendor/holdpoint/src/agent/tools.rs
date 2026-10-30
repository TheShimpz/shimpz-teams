//! Answering a response's tool calls: validation against the offered tools, then local computations and activities
//! in order, then every deferred call of the response in one durable batch.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{Value, json};

use super::context;
use super::entry::{Entry, OfferedTool, Part, ToolKind, ToolOutcome, ToolSpec};
use crate::ctx::Ctx;
use crate::definition::{Interrupt, StepResult};
use crate::policy::Failure;
use crate::schema::Schema;

/// A pure computation offered as a tool.
pub type LocalTool = Arc<dyn Fn(&Value) -> Result<Value, Failure> + Send + Sync>;

/// One tool call of a response.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Call {
    pub(crate) id: String,
    pub(crate) name: String,
    pub(crate) arguments: Value,
}

/// The tool calls of `parts`, in order.
pub(crate) fn calls(parts: &[Part]) -> Vec<Call> {
    parts
        .iter()
        .filter_map(|part| match part {
            Part::ToolCall { id, name, arguments } => {
                Some(Call { id: id.clone(), name: name.clone(), arguments: arguments.clone() })
            }
            Part::Text { .. } | Part::Opaque { .. } => None,
        })
        .collect()
}

/// How a response's calls were answered.
pub(crate) enum Answered {
    /// The response's only call was a valid final result.
    Final(Value),
    /// One result per call, in call order.
    Results(Vec<Entry>),
}

/// The offered tools with their compiled schemas.
pub struct Tools {
    specs: HashMap<String, (ToolSpec, Option<Schema>)>,
    offered: Vec<OfferedTool>,
}

impl Tools {
    /// Compiles every validated tool's schema.
    ///
    /// # Errors
    ///
    /// [`Interrupt::Unrecoverable`] when a schema is outside the subset or a name repeats.
    pub fn compile(specs: &[ToolSpec]) -> Result<Self, Interrupt> {
        let mut compiled = HashMap::new();
        for spec in specs {
            let schema = if spec.validate {
                Some(
                    Schema::compile(&spec.parameters)
                        .map_err(|_| Interrupt::Unrecoverable(format!("bad-tool-schema:{}", spec.name)))?,
                )
            } else {
                None
            };
            if compiled.insert(spec.name.clone(), (spec.clone(), schema)).is_some() {
                return Err(Interrupt::Unrecoverable(format!("repeated-tool:{}", spec.name)));
            }
        }
        Ok(Self { specs: compiled, offered: context::offered(specs) })
    }

    pub(crate) fn offered(&self) -> Vec<OfferedTool> {
        self.offered.clone()
    }

    /// Checks one call against the tool it names, before anything runs.
    fn check(&self, call: &Call) -> Result<&ToolSpec, ToolOutcome> {
        let (spec, schema) = self
            .specs
            .get(&call.name)
            .ok_or_else(|| ToolOutcome::error("unknown-tool", &format!("no tool is named {}", call.name)))?;
        if let Some(schema) = schema {
            schema
                .validate(&call.arguments)
                .map_err(|error| ToolOutcome::error("invalid-arguments", &error.to_string()))?;
        }
        Ok(spec)
    }

    pub(crate) fn answer(
        &self,
        ctx: &mut Ctx<'_>,
        locals: &HashMap<String, LocalTool>,
        calls: &[Call],
        max_calls: u32,
        progress: &dyn Fn(&[Entry]) -> Value,
    ) -> Result<Answered, Interrupt> {
        if let Some(refused) = repeated_ids(calls) {
            return Ok(refused);
        }
        if u32::try_from(calls.len()).unwrap_or(u32::MAX) > max_calls {
            let refused = ToolOutcome::error("too-many-calls", &format!("call at most {max_calls} tools at once"));
            return Ok(Answered::Results(every(calls, &refused)));
        }
        let finals = calls
            .iter()
            .filter(|call| self.specs.get(&call.name).is_some_and(|(spec, _)| spec.kind == ToolKind::Final));
        if finals.count() > 0 {
            return Ok(self.answer_final(calls));
        }
        let mut outcomes: Vec<Option<ToolOutcome>> = vec![None; calls.len()];
        let mut finished = Vec::new();
        let mut deferred = Vec::new();
        for (index, call) in calls.iter().enumerate() {
            match self.check(call) {
                Err(outcome) => outcomes[index] = Some(outcome),
                Ok(spec) if spec.kind == ToolKind::Deferred => deferred.push(index),
                Ok(spec) => {
                    let outcome = run(ctx, locals, spec, call)?;
                    finished.push(Entry::ToolResult { call_id: call.id.clone(), outcome: outcome.clone() });
                    // A later call may expire or be cancelled: what this one found is already in the progress.
                    ctx.set_progress(progress(&finished));
                    outcomes[index] = Some(outcome);
                }
            }
        }
        if !deferred.is_empty() {
            answer_deferred(ctx, calls, &deferred, &mut outcomes, &mut finished, progress)?;
        }
        let results = calls
            .iter()
            .zip(outcomes)
            .map(|(call, outcome)| Entry::ToolResult {
                call_id: call.id.clone(),
                outcome: outcome.unwrap_or(ToolOutcome::Uncertain),
            })
            .collect();
        Ok(Answered::Results(results))
    }

    fn answer_final(&self, calls: &[Call]) -> Answered {
        if let [call] = calls {
            return match self.check(call) {
                Ok(_) => Answered::Final(call.arguments.clone()),
                Err(outcome) => Answered::Results(every(calls, &outcome)),
            };
        }
        let refused = ToolOutcome::error("final-not-alone", "the final result must be the response's only tool call");
        Answered::Results(every(calls, &refused))
    }
}

/// Registers every deferred call of the response in one durable batch and fills in the answers; until all arrived,
/// the run waits, with the ones that did arrive already in its progress.
fn answer_deferred(
    ctx: &mut Ctx<'_>,
    calls: &[Call],
    deferred: &[usize],
    outcomes: &mut [Option<ToolOutcome>],
    finished: &mut Vec<Entry>,
    progress: &dyn Fn(&[Entry]) -> Value,
) -> Result<(), Interrupt> {
    let requests: Vec<(&str, Value)> = deferred
        .iter()
        .map(|&index| {
            let call = &calls[index];
            (call.name.as_str(), json!({ "call_id": call.id, "arguments": call.arguments }))
        })
        .collect();
    let answers = ctx.deferred_results(&requests)?;
    let complete = answers.iter().all(Option::is_some);
    for (&index, answer) in deferred.iter().zip(answers) {
        let Some(answer) = answer else {
            continue;
        };
        let outcome =
            serde_json::from_value(answer.clone()).unwrap_or(ToolOutcome::Ok { value: answer, images: vec![] });
        finished.push(Entry::ToolResult { call_id: calls[index].id.clone(), outcome: outcome.clone() });
        outcomes[index] = Some(outcome);
    }
    ctx.set_progress(progress(finished));
    if complete { Ok(()) } else { ctx.await_deferred() }
}

/// Results pair with calls by id: with a repeated id nothing runs, and each distinct id gets one error.
fn repeated_ids(calls: &[Call]) -> Option<Answered> {
    let mut ids: Vec<&str> = calls.iter().map(|call| call.id.as_str()).collect();
    ids.sort_unstable();
    if !ids.windows(2).any(|pair| pair[0] == pair[1]) {
        return None;
    }
    ids.dedup();
    let refused = ToolOutcome::error("duplicate-call-id", "every tool call needs its own id");
    let results =
        ids.into_iter().map(|id| Entry::ToolResult { call_id: id.to_owned(), outcome: refused.clone() }).collect();
    Some(Answered::Results(results))
}

fn every(calls: &[Call], outcome: &ToolOutcome) -> Vec<Entry> {
    calls.iter().map(|call| Entry::ToolResult { call_id: call.id.clone(), outcome: outcome.clone() }).collect()
}

fn run(
    ctx: &mut Ctx<'_>,
    locals: &HashMap<String, LocalTool>,
    spec: &ToolSpec,
    call: &Call,
) -> Result<ToolOutcome, Interrupt> {
    let result = if spec.kind == ToolKind::Local {
        let Some(tool) = locals.get(&spec.name) else {
            return Ok(ToolOutcome::error("unavailable", &format!("tool {} has no implementation", spec.name)));
        };
        let tool = Arc::clone(tool);
        ctx.local(&spec.name, &call.arguments, move |arguments| tool(arguments))?
    } else {
        ctx.activity(&spec.name, call.arguments.clone())?
    };
    Ok(match result {
        StepResult::Ok(output) => ToolOutcome::Ok { value: output.value, images: output.blobs },
        StepResult::Failed(failure) => ToolOutcome::Error { code: failure.code, message: failure.message },
        StepResult::Uncertain => ToolOutcome::Uncertain,
    })
}
