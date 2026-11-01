//! The closed cause of a failed Team Action: exactly what Team observed, so a failure is diagnosable without
//! investigation.
//!
//! A cause names its `kind` (the part of the system that failed), its `condition` (what Team observed, which belongs to
//! exactly one kind), and its `phase` (when), with narrowly typed parameters: an exit code, an HTTP status, a byte
//! count, and whether the workload wrote to stderr and how much. It keeps the initiating failure apart from the cleanup
//! it caused (`cleanup`), such as a workload whose stop could not be confirmed. Every text is one of these closed words,
//! so a cause carries no credential, prompt, input, result, file name, header, body, URL, provider text, exception,
//! child output, or attacker-chosen member name or value; a validation cause names only the rule that failed. A cause
//! is at most [`MAX_CAUSE_BYTES`] serialized and renders within [`MAX_RENDERED_CHARS`].
//!
//! A cause describes; it never decides. Its precision never changes an outcome's uncertainty, never authorizes a
//! retry, and never claims that a provider call had no effect: those stay with the operation's own result and code.

use strict_json::Value;

use crate::python::json::{self, Options};
use crate::python::value::{field, int, object, small_int, string, text};

/// The longest serialized cause.
pub const MAX_CAUSE_BYTES: usize = 1024;
/// The longest rendered cause.
pub const MAX_RENDERED_CHARS: usize = 512;
/// The largest byte count a cause states.
pub const MAX_CAUSE_COUNT: i64 = (1 << 31) - 1;
/// The members a cause may carry, in their canonical order.
pub const CAUSE_MEMBERS: [&str; 8] = [
    "kind",
    "condition",
    "phase",
    "exit_code",
    "http_status",
    "bytes",
    "stderr_bytes",
    "cleanup",
];
/// What a rendered cause says when there is none.
pub const UNKNOWN_CAUSE: &str = "cause unknown";

macro_rules! closed {
    ($(#[$meta:meta])* $name:ident { $($(#[$variant_meta:meta])* $variant:ident = $text:literal,)* }) => {
        $(#[$meta])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq)]
        pub enum $name {
            $($(#[$variant_meta])* $variant,)*
        }

        impl $name {
            /// Every value, in declaration order.
            pub const ALL: &'static [Self] = &[$(Self::$variant,)*];

            /// Its wire text.
            #[must_use]
            pub const fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $text,)*
                }
            }

            /// The value whose wire text is `text`.
            #[must_use]
            pub fn parse(text: &str) -> Option<Self> {
                Self::ALL.iter().copied().find(|value| value.as_str() == text)
            }
        }
    };
}

closed! {
    /// The part of the system that failed.
    Kind {
        /// Team refused the request before anything ran: the Action, its input, or its binding.
        ActionAdmission = "action-admission",
        /// The Action asked something the route cannot ask.
        AssistantRequest = "assistant-request",
        /// Team's own audit log did not record what Team was about to do, so Team did not do it.
        TeamAudit = "team-audit",
        /// Team's own durable state could not be read or written.
        TeamState = "team-state",
        /// Docker could not run the call.
        Docker = "docker",
        /// An Integration the Action declares could not be resolved.
        Integration = "integration",
        /// The file the Action names could not be given to it.
        File = "file",
        /// Stop ended the work.
        Stop = "stop",
        /// The workload is blocked since an earlier failure could not be proven stopped.
        Workload = "workload",
        /// The exec channel to the workload failed.
        AssistantTransport = "assistant-transport",
        /// The one deadline of the call passed.
        AssistantTimeout = "assistant-timeout",
        /// The workload process did not exit cleanly.
        AssistantExit = "assistant-exit",
        /// The workload's output is not an admitted frame, request, or result.
        AssistantOutputInvalid = "assistant-output-invalid",
        /// The workload's output echoed a value Team injected.
        AssistantSecretEcho = "assistant-secret-echo",
        /// The Action answered a closed outcome that is a failure.
        AssistantAnswer = "assistant-answer",
    }
}

closed! {
    /// What Team observed.
    Condition {
        /// The Assistant does not declare the Action.
        ActionNotDeclared = "action-not-declared",
        /// The input does not satisfy the Action's reviewed schema.
        InputInvalid = "input-invalid",
        /// The Assistant is not installed or running, or its workload changed since the batch was bound.
        ContextChanged = "context-changed",
        /// The invocation is beyond its bound.
        InvocationTooLarge = "invocation-too-large",
        /// An earlier attempt of the operation may have taken effect, so it never runs again.
        AttemptUncertain = "attempt-uncertain",
        /// The Action needs a person, which the direct route cannot ask.
        HumanRequestRequired = "human-request-required",
        /// The audit log refused the record as inadmissible.
        RecordRefused = "record-refused",
        /// The audit log could not write the record.
        WriteFailed = "write-failed",
        /// The audit log is closed after an earlier failed sync.
        JournalClosed = "journal-closed",
        /// The Action journal could not be read or written.
        JournalUnavailable = "journal-unavailable",
        /// A Stored Input or Integration store could not be read.
        StoredInputUnavailable = "stored-input-unavailable",
        /// A Routine run could not seal the dispatch.
        DispatchSealRefused = "dispatch-seal-refused",
        /// No operation id could be drawn.
        OperationIdUnavailable = "operation-id-unavailable",
        /// Docker could not answer.
        DockerUnavailable = "docker-unavailable",
        /// No Docker call slot freed before the deadline; nothing was sent.
        SlotTimeout = "slot-timeout",
        /// The Integration is not connected or could not be refreshed.
        IntegrationUnavailable = "integration-unavailable",
        /// The file is not one the turn selected, or no longer has its bytes.
        FileUnavailable = "file-unavailable",
        /// Another file delivery held the one file slot past the deadline.
        FileBusy = "file-busy",
        /// Stop arrived.
        Stopped = "stopped",
        /// The workload is blocked.
        WorkloadBlocked = "workload-blocked",
        /// The exec could not be started.
        ExecStartFailed = "exec-start-failed",
        /// The exec stream failed while it was read.
        StreamFailed = "stream-failed",
        /// A reply to a provider call could not be written to the workload.
        ReplyWriteFailed = "reply-write-failed",
        /// The deadline passed.
        DeadlinePassed = "deadline-passed",
        /// The process exited with a non-zero status (`exit_code`).
        ExitStatus = "exit-status",
        /// The exit status could not be read.
        ExitUnavailable = "exit-unavailable",
        /// The process still ran after its stream ended.
        StillRunning = "still-running",
        /// The process exited cleanly but wrote to stderr (`stderr_bytes`).
        StderrOutput = "stderr-output",
        /// An output frame named a stream other than stdout or stderr.
        StreamUnknown = "stream-unknown",
        /// The output passed its bound (`bytes`).
        OutputTooLarge = "output-too-large",
        /// The output ended inside a frame.
        FrameTruncated = "frame-truncated",
        /// An output line is not strict JSON.
        LineNotJson = "line-not-json",
        /// Output followed the terminal line.
        OutputAfterTerminal = "output-after-terminal",
        /// The terminal line is not a JSON object.
        TerminalNotObject = "terminal-not-object",
        /// A request followed a provider call, which a replay would repeat.
        RequestAfterProviderCall = "request-after-provider-call",
        /// The terminal frame's type is not an admitted one.
        FrameTypeInvalid = "frame-type-invalid",
        /// The terminal frame's members are not its type's exact set.
        FrameMembersInvalid = "frame-members-invalid",
        /// A handled failure is not an admitted failure document.
        FailureInvalid = "failure-invalid",
        /// A human request failed its admission.
        RequestInvalid = "request-invalid",
        /// A human request asked again for an authorization the transcript holds.
        AuthorizationRepeated = "authorization-repeated",
        /// A human request asked for a Stored Input Team holds.
        HeldInputRequested = "held-input-requested",
        /// The workload rejected a Stored Input it does not declare or Team does not hold.
        StoredInputNotHeld = "stored-input-not-held",
        /// A file-taking Action whose file was withheld answered a result.
        ResultWithWithheldFile = "result-with-withheld-file",
        /// The result is not a JSON object.
        ResultNotObject = "result-not-object",
        /// The result nests beyond its bound.
        ResultTooDeep = "result-too-deep",
        /// The result does not match the Action's reviewed output schema.
        ResultSchemaMismatch = "result-schema-mismatch",
        /// A pattern of the output schema could not be evaluated.
        SchemaPatternUnevaluable = "schema-pattern-unevaluable",
        /// A number of the result cannot be compared as the output schema requires.
        ResultNumberUnevaluable = "result-number-unevaluable",
        /// The output schema uses an unsupported keyword.
        SchemaUnsupported = "schema-unsupported",
        /// The result is beyond the journal's durable bound.
        ResultNotDurable = "result-not-durable",
        /// The output held a value Team injected.
        InjectedValueEchoed = "injected-value-echoed",
        /// The Action answered a handled failure (`http_status`, the provider's, when it gave one).
        HandledFailure = "handled-failure",
        /// The Assistant rejected a held Stored Input, which Team cleared.
        StoredInputRejected = "stored-input-rejected",
    }
}

closed! {
    /// When the failure happened.
    Phase {
        /// Admitting the request: its binding, Action, and input.
        Admission = "admission",
        /// Resolving the private values and files and encoding the invocation.
        Preparation = "preparation",
        /// Waiting for the one file slot.
        FileSlot = "file-slot",
        /// Reading or writing the Action journal around the attempt.
        Journal = "journal",
        /// Recording the dispatch before anything is sent.
        DispatchRecord = "dispatch-record",
        /// Waiting for a Docker call slot.
        SlotAdmission = "slot-admission",
        /// Starting the exec.
        ExecStart = "exec-start",
        /// Exchanging the invocation and the output.
        Exchange = "exchange",
        /// Answering one of the workload's provider calls.
        ProviderCall = "provider-call",
        /// Reading the exit status.
        ExitInspection = "exit-inspection",
        /// Admitting the terminal frame.
        ResultValidation = "result-validation",
        /// Recording the attempt's outcome in the journal.
        Settlement = "settlement",
    }
}

closed! {
    /// What became of the cleanup the failure caused.
    Cleanup {
        /// The workload was proven stopped.
        WorkloadStopped = "workload-stopped",
        /// The workload could not be proven stopped; nothing more runs in it.
        WorkloadStopUnconfirmed = "workload-stop-unconfirmed",
        /// The journal could not record the attempt's outcome.
        JournalUnavailable = "journal-unavailable",
    }
}

impl Condition {
    /// The one kind this condition belongs to.
    #[must_use]
    pub const fn kind(self) -> Kind {
        match self {
            Self::ActionNotDeclared
            | Self::InputInvalid
            | Self::ContextChanged
            | Self::InvocationTooLarge
            | Self::AttemptUncertain => Kind::ActionAdmission,
            Self::HumanRequestRequired => Kind::AssistantRequest,
            Self::RecordRefused | Self::WriteFailed | Self::JournalClosed => Kind::TeamAudit,
            Self::JournalUnavailable
            | Self::StoredInputUnavailable
            | Self::DispatchSealRefused
            | Self::OperationIdUnavailable => Kind::TeamState,
            Self::DockerUnavailable | Self::SlotTimeout => Kind::Docker,
            Self::IntegrationUnavailable => Kind::Integration,
            Self::FileUnavailable | Self::FileBusy => Kind::File,
            Self::Stopped => Kind::Stop,
            Self::WorkloadBlocked => Kind::Workload,
            Self::ExecStartFailed | Self::StreamFailed | Self::ReplyWriteFailed => {
                Kind::AssistantTransport
            }
            Self::DeadlinePassed => Kind::AssistantTimeout,
            Self::ExitStatus | Self::ExitUnavailable | Self::StillRunning | Self::StderrOutput => {
                Kind::AssistantExit
            }
            Self::InjectedValueEchoed => Kind::AssistantSecretEcho,
            Self::HandledFailure | Self::StoredInputRejected => Kind::AssistantAnswer,
            Self::StreamUnknown
            | Self::OutputTooLarge
            | Self::FrameTruncated
            | Self::LineNotJson
            | Self::OutputAfterTerminal
            | Self::TerminalNotObject
            | Self::RequestAfterProviderCall
            | Self::FrameTypeInvalid
            | Self::FrameMembersInvalid
            | Self::FailureInvalid
            | Self::RequestInvalid
            | Self::AuthorizationRepeated
            | Self::HeldInputRequested
            | Self::StoredInputNotHeld
            | Self::ResultWithWithheldFile
            | Self::ResultNotObject
            | Self::ResultTooDeep
            | Self::ResultSchemaMismatch
            | Self::SchemaPatternUnevaluable
            | Self::ResultNumberUnevaluable
            | Self::SchemaUnsupported
            | Self::ResultNotDurable => Kind::AssistantOutputInvalid,
        }
    }

    /// Whether a cause of this condition may state the workload's stderr byte count.
    const fn states_stderr(self) -> bool {
        matches!(self.kind(), Kind::AssistantExit)
    }
}

/// One closed cause; its parameters always fit its condition.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cause {
    condition: Condition,
    phase: Phase,
    exit_code: Option<i32>,
    http_status: Option<u16>,
    bytes: Option<u32>,
    stderr_bytes: Option<u32>,
    cleanup: Option<Cleanup>,
}

/// A count within the bound a cause states.
fn count(value: usize) -> u32 {
    u32::try_from(value)
        .unwrap_or(u32::MAX)
        .min(u32::try_from(MAX_CAUSE_COUNT).unwrap_or(u32::MAX))
}

impl Cause {
    /// A cause of `condition` in `phase`; a condition that requires a parameter takes its own constructor.
    #[must_use]
    pub const fn new(condition: Condition, phase: Phase) -> Self {
        Self {
            condition,
            phase,
            exit_code: None,
            http_status: None,
            bytes: None,
            stderr_bytes: None,
            cleanup: None,
        }
    }

    /// A process that exited with `exit_code`, read in [`Phase::ExitInspection`].
    #[must_use]
    pub fn exit_status(exit_code: i64) -> Self {
        let clamped = exit_code.clamp(i64::from(i32::MIN), i64::from(i32::MAX));
        Self {
            exit_code: Some(i32::try_from(clamped).unwrap_or_default()),
            ..Self::new(Condition::ExitStatus, Phase::ExitInspection)
        }
    }

    /// A process that exited cleanly after writing `stderr_bytes` (at least one) to stderr.
    #[must_use]
    pub fn stderr_output(stderr_bytes: usize) -> Self {
        Self {
            stderr_bytes: Some(count(stderr_bytes.max(1))),
            ..Self::new(Condition::StderrOutput, Phase::ExitInspection)
        }
    }

    /// Output that passed its bound at `bytes`, observed in `phase`.
    #[must_use]
    pub fn output_too_large(bytes: usize, phase: Phase) -> Self {
        Self {
            bytes: Some(count(bytes)),
            ..Self::new(Condition::OutputTooLarge, phase)
        }
    }

    /// A handled failure, with the provider's HTTP status when it gave a valid one.
    #[must_use]
    pub fn handled(http_status: Option<i64>) -> Self {
        Self {
            http_status: http_status
                .filter(|status| (100..=599).contains(status))
                .and_then(|status| u16::try_from(status).ok()),
            ..Self::new(Condition::HandledFailure, Phase::ResultValidation)
        }
    }

    /// The cause with the workload's stderr byte count, when its condition states one and it wrote any.
    #[must_use]
    pub fn with_stderr(self, stderr_bytes: usize) -> Self {
        if !self.condition.states_stderr() || stderr_bytes == 0 {
            return self;
        }
        Self {
            stderr_bytes: Some(count(stderr_bytes)),
            ..self
        }
    }

    /// The cause with what became of its cleanup.
    #[must_use]
    pub const fn with_cleanup(self, cleanup: Cleanup) -> Self {
        Self {
            cleanup: Some(cleanup),
            ..self
        }
    }

    /// Its kind.
    #[must_use]
    pub const fn kind(&self) -> Kind {
        self.condition.kind()
    }

    /// Its condition.
    #[must_use]
    pub const fn condition(&self) -> Condition {
        self.condition
    }

    /// Its phase.
    #[must_use]
    pub const fn phase(&self) -> Phase {
        self.phase
    }

    /// Its exit code.
    #[must_use]
    pub const fn exit_code(&self) -> Option<i32> {
        self.exit_code
    }

    /// Its stderr byte count.
    #[must_use]
    pub const fn stderr_bytes(&self) -> Option<u32> {
        self.stderr_bytes
    }

    /// What became of its cleanup.
    #[must_use]
    pub const fn cleanup(&self) -> Option<Cleanup> {
        self.cleanup
    }

    /// Its canonical wire object: `kind`, `condition`, `phase`, then each parameter it states, in [`CAUSE_MEMBERS`]
    /// order.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut members = vec![
            ("kind".to_owned(), text(self.kind().as_str())),
            ("condition".to_owned(), text(self.condition.as_str())),
            ("phase".to_owned(), text(self.phase.as_str())),
        ];
        let numbers = [
            ("exit_code", self.exit_code.map(i64::from)),
            ("http_status", self.http_status.map(i64::from)),
            ("bytes", self.bytes.map(i64::from)),
            ("stderr_bytes", self.stderr_bytes.map(i64::from)),
        ];
        members.extend(
            numbers
                .into_iter()
                .filter_map(|(name, number)| Some((name.to_owned(), int(number?)))),
        );
        if let Some(cleanup) = self.cleanup {
            members.push(("cleanup".to_owned(), text(cleanup.as_str())));
        }
        Value::Object(members)
    }

    /// The cause `value` states exactly: a closed object whose kind is its condition's, whose words are each closed,
    /// and whose parameters are exactly the ones its condition admits, within their bounds.
    #[must_use]
    pub fn from_value(value: &Value) -> Option<Self> {
        let members = object(value)?;
        if members.len() > CAUSE_MEMBERS.len()
            || members
                .iter()
                .any(|(name, _)| !CAUSE_MEMBERS.contains(&name.as_str()))
        {
            return None;
        }
        let word = |name: &str| string(field(members, name));
        let condition = Condition::parse(word("condition")?)?;
        let phase = Phase::parse(word("phase")?)?;
        if word("kind")? != condition.kind().as_str() {
            return None;
        }
        let present = |name: &str| members.iter().any(|(key, _)| key == name);
        let number = |name: &str, low: i64, high: i64| -> Option<Option<i64>> {
            if !present(name) {
                return Some(None);
            }
            small_int(field(members, name))
                .filter(|number| (low..=high).contains(number))
                .map(Some)
        };
        let exit_code = number("exit_code", i64::from(i32::MIN), i64::from(i32::MAX))?;
        let http_status = number("http_status", 100, 599)?;
        let bytes = number("bytes", 0, MAX_CAUSE_COUNT)?;
        let stderr_bytes = number("stderr_bytes", 1, MAX_CAUSE_COUNT)?;
        let cleanup = if present("cleanup") {
            Some(Cleanup::parse(word("cleanup")?)?)
        } else {
            None
        };
        let fits = exit_code.is_some() == (condition == Condition::ExitStatus)
            && bytes.is_some() == (condition == Condition::OutputTooLarge)
            && (http_status.is_none() || condition == Condition::HandledFailure)
            && (stderr_bytes.is_none() || condition.states_stderr())
            && (stderr_bytes.is_some() || condition != Condition::StderrOutput);
        if !fits {
            return None;
        }
        let narrow = |number: Option<i64>| number.and_then(|number| u32::try_from(number).ok());
        Some(Self {
            condition,
            phase,
            exit_code: exit_code.and_then(|code| i32::try_from(code).ok()),
            http_status: http_status.and_then(|status| u16::try_from(status).ok()),
            bytes: narrow(bytes),
            stderr_bytes: narrow(stderr_bytes),
            cleanup,
        })
    }

    /// Its one line for a person: `kind · condition · phase`, then each parameter and the cleanup.
    #[must_use]
    pub fn render(&self) -> String {
        let mut parts = vec![
            self.kind().as_str().to_owned(),
            self.condition.as_str().to_owned(),
            self.phase.as_str().to_owned(),
        ];
        let numbers = [
            ("exit-code", self.exit_code.map(i64::from)),
            ("http-status", self.http_status.map(i64::from)),
            ("bytes", self.bytes.map(i64::from)),
            ("stderr-bytes", self.stderr_bytes.map(i64::from)),
        ];
        parts.extend(
            numbers
                .into_iter()
                .filter_map(|(name, number)| Some(format!("{name} {}", number?))),
        );
        if let Some(cleanup) = self.cleanup {
            parts.push(cleanup.as_str().to_owned());
        }
        parts.join(" · ")
    }
}

/// `canonical_cause`: the canonical object (members in [`CAUSE_MEMBERS`] order) of exactly one closed cause within
/// [`MAX_CAUSE_BYTES`], read in any member order, or `None`.
#[must_use]
pub fn canonical_cause(value: &Value) -> Option<Value> {
    const COMPACT: Options = Options {
        ensure_ascii: true,
        sort_keys: false,
        compact: true,
    };
    Cause::from_value(value)
        .map(|cause| cause.to_value())
        .filter(|canonical| json::dumps(canonical, COMPACT).len() <= MAX_CAUSE_BYTES)
}

/// `render_cause`: the rendered cause, or [`UNKNOWN_CAUSE`] when Team stated none.
#[must_use]
pub fn render_cause(cause: Option<&Cause>) -> String {
    cause.map_or_else(|| UNKNOWN_CAUSE.to_owned(), Cause::render)
}

#[cfg(test)]
mod tests;
