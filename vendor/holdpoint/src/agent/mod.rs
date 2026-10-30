//! The agent loop as one durable definition, after Pi's minimal loop: model, then every tool call answered, then the
//! model again, until a plain reply, a final result, or a bound.
//!
//! Every model call (folds included), tool computation, effect, and human request is a journaled step, so an agent
//! run resumes exactly where it stopped. Every tool call gets exactly one result before the next model call (R6), a
//! truncated response is never accepted (R10), and a final result must be the response's only call.

mod context;
pub mod entry;
mod tools;

use std::collections::HashMap;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::ctx::Ctx;
use crate::definition::{Definition, Interrupt};
use crate::error::LimitKind;
use crate::event::Outcome;
use crate::policy::Failure;
use entry::{Entry, ModelRequest, ModelResponse, StopReason, ToolSpec};

pub use tools::{LocalTool, Tools};

/// One model call the agent makes.
struct Call<'a> {
    entries: &'a [Entry],
    tools: Vec<entry::OfferedTool>,
    system: &'a str,
    /// The transient images this call is the last consumer of.
    release: Vec<u64>,
    /// The images shown with this call.
    images: Vec<(String, u64)>,
}

/// The definition id every agent run uses.
pub const AGENT: &str = "agent";

/// An agent run's captured input.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentInput {
    /// The registered model handler.
    pub model: String,
    /// The provider's model name.
    pub model_name: String,
    /// The system prompt.
    pub system: String,
    /// The transcript the run starts from.
    pub messages: Vec<Entry>,
    /// The tools offered to every call.
    pub tools: Vec<ToolSpec>,
    /// The run's bounds.
    #[serde(default)]
    pub limits: AgentLimits,
    /// The output bound of each call.
    #[serde(default = "default_output")]
    pub max_output_tokens: u32,
    /// The provider's reasoning effort.
    #[serde(default)]
    pub effort: Option<String>,
    /// Folding of a long transcript, when set.
    #[serde(default)]
    pub fold: Option<Fold>,
}

fn default_output() -> u32 {
    4_096
}

/// The agent's bounds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentLimits {
    /// Model calls before the run must give its final answer (one more call, without tools, gives it).
    pub max_model_steps: u32,
    /// Tool calls one response may make.
    pub max_tool_calls: u32,
    /// The transcript's encoded size.
    pub max_transcript_bytes: u64,
}

impl Default for AgentLimits {
    fn default() -> Self {
        Self { max_model_steps: 24, max_tool_calls: 16, max_transcript_bytes: 1 << 20 }
    }
}

/// When and how a long transcript is folded into a memory by a journaled model call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fold {
    /// Fold once the transcript's encoded size passes this.
    pub at_bytes: u64,
    /// Entries kept verbatim after the fold.
    pub keep_last: usize,
    /// The instruction the folding call receives as its system prompt.
    pub instruction: String,
}

/// A deterministic rewrite of the transcript before each call (omission, redaction); part of replay.
pub type Transform = Arc<dyn Fn(&[Entry]) -> Vec<Entry> + Send + Sync>;

/// The agent definition. Local tools and the transform are code: change them only with a new version.
pub struct Agent {
    version: u32,
    locals: HashMap<String, LocalTool>,
    transform: Option<Transform>,
}

/// Where the loop stands. The transcript's encoded size is kept as entries are added, so a long run is never
/// re-serialized at every step.
struct Turn {
    entries: Vec<Entry>,
    sizes: Vec<u64>,
    steps: u32,
    /// Images a fold removed from the transcript, released when the next call commits.
    orphaned: Vec<u64>,
}

impl Turn {
    fn new(entries: Vec<Entry>) -> Self {
        let sizes = entries.iter().map(context::entry_bytes).collect();
        Self { entries, sizes, steps: 0, orphaned: vec![] }
    }

    fn push(&mut self, entry: Entry) {
        self.sizes.push(context::entry_bytes(&entry));
        self.entries.push(entry);
    }

    /// The transcript's canonical size: brackets, entries, and the commas between them.
    fn bytes(&self) -> u64 {
        let separators = u64::try_from(self.sizes.len().saturating_sub(1)).unwrap_or(u64::MAX);
        2 + self.sizes.iter().sum::<u64>() + separators
    }

    fn replace(&mut self, entries: Vec<Entry>, orphaned: Vec<u64>) {
        let mut orphaned = orphaned;
        orphaned.extend(std::mem::take(&mut self.orphaned));
        *self = Self { steps: self.steps, orphaned, ..Self::new(entries) };
    }

    /// The images the next call is the last consumer of: the newest tool results' and the folded ones'.
    fn release(&self) -> (Vec<(String, u64)>, Vec<u64>) {
        let fresh = context::fresh_images(&self.entries);
        let release = fresh.iter().map(|(_, id)| *id).chain(self.orphaned.iter().copied()).collect();
        (fresh, release)
    }
}

impl Agent {
    /// An agent definition at `version`.
    #[must_use]
    pub fn new(version: u32) -> Self {
        Self { version, locals: HashMap::new(), transform: None }
    }

    /// Registers the local tool `name`.
    #[must_use]
    pub fn local_tool(
        mut self,
        name: &str,
        tool: impl Fn(&Value) -> Result<Value, Failure> + Send + Sync + 'static,
    ) -> Self {
        self.locals.insert(name.to_owned(), Arc::new(tool));
        self
    }

    /// Sets the transcript transform.
    #[must_use]
    pub fn transform(mut self, transform: impl Fn(&[Entry]) -> Vec<Entry> + Send + Sync + 'static) -> Self {
        self.transform = Some(Arc::new(transform));
        self
    }

    /// The run's durable partial progress: its latest text, its folded memory, and its newest tool findings, each
    /// bounded, so a limited, expired, or cancelled run still hands over what it learned.
    fn progress(turn: &Turn) -> Value {
        Self::progress_with(turn, &[])
    }

    /// The progress of `turn` with the results of the current response's calls that already finished.
    fn progress_with(turn: &Turn, finished: &[Entry]) -> Value {
        let entries: Vec<&Entry> = turn.entries.iter().chain(finished).collect();
        let last = entries.iter().rev().find_map(|entry| match entry {
            Entry::Assistant { parts } => Some(context::bounded_text(&entry::text_of(parts))),
            _ => None,
        });
        let memory = entries.iter().find_map(|entry| match entry {
            Entry::Memory { text } => Some(context::bounded_text(text)),
            _ => None,
        });
        json!({ "model_steps": turn.steps, "last_text": last, "memory": memory, "findings": context::findings(&entries) })
    }

    fn step(
        &self,
        ctx: &mut Ctx<'_>,
        input: &AgentInput,
        tools: &Tools,
        turn: &mut Turn,
    ) -> Result<Option<Outcome>, Interrupt> {
        self.fold(ctx, input, turn)?;
        if turn.steps >= input.limits.max_model_steps {
            return self.answer_at_limit(ctx, input, turn).map(Some);
        }
        if turn.bytes() > input.limits.max_transcript_bytes {
            return Ok(Some(Outcome::Limited { kind: LimitKind::History, progress: Self::progress(turn) }));
        }
        let (images, release) = turn.release();
        let call = Call { entries: &turn.entries, tools: tools.offered(), system: &input.system, release, images };
        let response = match self.call(ctx, input, call)? {
            Ok(response) => response,
            Err(outcome) => return Ok(Some(outcome)),
        };
        turn.orphaned.clear();
        turn.steps += 1;
        if response.stop == StopReason::MaxTokens {
            return Ok(Some(Outcome::Limited { kind: LimitKind::Output, progress: Self::progress(turn) }));
        }
        let calls = tools::calls(&response.parts);
        let text = response.text();
        turn.push(Entry::Assistant { parts: response.parts });
        // The answer is in the progress before anything else can end the run.
        ctx.set_progress(Self::progress(turn));
        if calls.is_empty() {
            let key = if response.stop == StopReason::Refusal { "refused" } else { "reply" };
            return Ok(Some(Outcome::Done { result: json!({ key: text }) }));
        }
        let progress = |finished: &[Entry]| Self::progress_with(turn, finished);
        match tools.answer(ctx, &self.locals, &calls, input.limits.max_tool_calls, &progress)? {
            tools::Answered::Final(arguments) => {
                let mut progress = Self::progress(turn);
                progress["final"] = context::bounded(&arguments);
                ctx.set_progress(progress);
                Ok(Some(Outcome::Done { result: json!({ "final": arguments }) }))
            }
            tools::Answered::Results(results) => {
                for result in results {
                    turn.push(result);
                }
                Ok(None)
            }
        }
    }

    /// One journaled model call; a failed call ends the run.
    fn call(
        &self,
        ctx: &mut Ctx<'_>,
        input: &AgentInput,
        call: Call<'_>,
    ) -> Result<Result<ModelResponse, Outcome>, Interrupt> {
        let Call { entries, tools, system, release, images } = call;
        let result = ctx.model(&input.model, &release, |blobs| {
            let shown = self.transform.as_ref().map_or_else(|| entries.to_vec(), |transform| transform(entries));
            let request = ModelRequest {
                model: input.model_name.clone(),
                system: system.to_owned(),
                entries: shown,
                images: context::images(blobs, &images)?,
                tools,
                max_output_tokens: input.max_output_tokens,
                effort: input.effort.clone(),
            };
            serde_json::to_value(request).map_err(|_| Interrupt::Unrecoverable("bad-model-request".into()))
        })?;
        Ok(match result {
            Ok(output) => serde_json::from_value(output.value)
                .map_err(|_| Outcome::Unrecoverable { reason: "bad-model-response".into() }),
            Err(failure) => Err(Outcome::Unrecoverable { reason: format!("model-failed:{}", failure.code) }),
        })
    }

    /// The step bound was reached: one last call without tools gives the final answer.
    fn answer_at_limit(&self, ctx: &mut Ctx<'_>, input: &AgentInput, turn: &mut Turn) -> Result<Outcome, Interrupt> {
        turn.push(Entry::Notice { text: "Step limit reached: give your best final answer now, without tools.".into() });
        let (images, release) = turn.release();
        let call = Call { entries: &turn.entries, tools: vec![], system: &input.system, release, images };
        let answer = match self.call(ctx, input, call)? {
            Ok(response) if response.stop != StopReason::MaxTokens => Some(response.text()),
            Ok(_) | Err(_) => None,
        };
        let mut progress = Self::progress(turn);
        progress["answer"] = json!(answer);
        // Whatever ends the run now (a cancellation, the deadline), the final answer is part of its progress.
        ctx.set_progress(progress.clone());
        Ok(Outcome::Limited { kind: LimitKind::Steps, progress })
    }

    /// Folds the transcript's older entries into a memory notice once it grows past the fold's size.
    fn fold(&self, ctx: &mut Ctx<'_>, input: &AgentInput, turn: &mut Turn) -> Result<(), Interrupt> {
        let Some(fold) = &input.fold else {
            return Ok(());
        };
        if turn.bytes() <= fold.at_bytes {
            return Ok(());
        }
        let Some(cut) = context::fold_cut(&turn.entries, fold.keep_last) else {
            return Ok(());
        };
        let older = turn.entries[..cut].to_vec();
        // The fold sees no images and releases none: if it fails, the transcript still needs them.
        let call = Call { entries: &older, tools: vec![], system: &fold.instruction, release: vec![], images: vec![] };
        let memory = match self.call(ctx, input, call)? {
            Ok(response) if response.stop != StopReason::MaxTokens => response.text(),
            Ok(_) | Err(_) => return Ok(()),
        };
        let kept = turn.entries.split_off(cut);
        turn.replace(std::iter::once(Entry::Memory { text: memory }).chain(kept).collect(), context::images_of(&older));
        Ok(())
    }
}

impl Definition for Agent {
    fn id(&self) -> &'static str {
        AGENT
    }

    fn version(&self) -> u32 {
        self.version
    }

    fn run(&self, ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt> {
        let input: AgentInput = serde_json::from_value(ctx.input().clone())
            .map_err(|_| Interrupt::Unrecoverable("bad-agent-input".into()))?;
        let tools = Tools::compile(&input.tools)?;
        let mut turn = Turn::new(input.messages.clone());
        loop {
            ctx.set_progress(Self::progress(&turn));
            if let Some(outcome) = self.step(ctx, &input, &tools, &mut turn)? {
                return Ok(outcome);
            }
        }
    }
}
