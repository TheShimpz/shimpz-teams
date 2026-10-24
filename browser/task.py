"""One Browser task: Team's loop between the Browser agent's steps and the Cua Driver (ADR-0117 §5, 2026-10-22).

Each step sends the Brain operation `browser-step` the objective, the language, the task's thread (`thread.py`), the
current bounded observation, its downloads, and at most one screenshot; admits exactly one step from the closed set;
checks a targeted ref against the observation it was chosen from; executes it; and appends the step and its outcome to
the thread, folding the oldest steps and having the agent rewrite its task memory (`browser-fold`) before an entry
would pass the history's bound. Before every Brain call and every page action the task proves it may continue: no
Stop, time and spend left, and the Browser still on the egress process it started on. No call outlives the task's
deadline.

Spend is a ceiling of reservations: before each Brain call Team reserves every provider attempt at its worst case and
settles only the attempts whose usage the Brain reports. The last FINISH_SECONDS and FINISH_SHARE of the spend are kept
for finishing: past them there is never another step, and the Brain answers from the thread (`browser-final`). A done
answer is screened for provenance (`screen.py`) and judged complete by the Brain's advisory check (`browser-check`);
either may return it to the agent a bounded number of times, and what remains travels with the result.

A Cua Driver session that fails while the Browser is intact is replaced by a fresh one on the same Browser, at most
MAX_RECOVERIES times, and the task continues where it was with its whole thread: an action under way when it failed is
recorded as `interrupted`, since its effect is unknown. Every failure keeps its closed cause for the audit.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from browser import cua, read, screen
from browser import thread as browser_thread
from protocol.http.v1 import browser as browser_protocol

MAX_STEPS = browser_protocol.MAX_STEPS
MAX_SECONDS = float(browser_protocol.MAX_TASK_SECONDS)
# US$0.50 in millionths of a cent: a token count times a catalog price in cents per million tokens.
MAX_SPEND = 50 * 1_000_000
FINISH_SECONDS = 60.0
# The share kept for finishing covers a final answer from a thread at every bound, each attempt at the dearest Browser
# model's price, and the completeness check after it.
FINISH_SHARE = 16
# A request's input bound beyond its text: a screenshot of the fixed 1280x800 window on either provider, and the Brain's
# system prompt, step schema, and the instruction a fold, check, or final answer adds.
IMAGE_TOKENS = 3_000
PROMPT_TOKENS = 10_000
SCREENSHOT_EVERY = 3
WAIT_SECONDS = 2.0
MAX_RECOVERIES = 3
MAX_RETURNS_UNSCREENED = 2
MAX_RETURNS_INCOMPLETE = 4
# Which observed action a step needs on the ref it names.
REF_ACTIONS = {"click": "click", "type": "type"}
UNSCREENED_NOTE = "Open each of these URLs to confirm it, or leave it out of your answer; no page of this task showed: "
INCOMPLETE_NOTE = (
    "Not finished. If sources you have not read yet may hold it, keep gathering there; if you covered them all, answer "
    "done again with what you verified and what is missing. Missing: "
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """How a task ended: `done`, `needs-person`, `stopped`, `budget`, or `failed`, with its closed details.

    `cause` names, for the audit only, where a failure came from and its closed code; `recovered` counts the Driver
    sessions replaced on the way. A done answer carries what screening and the completeness check left unresolved.
    """

    status: str
    summary: str | None = None
    reason: str | None = None
    sentence: str | None = None
    steps: int = 0
    cause: str | None = None
    recovered: int = 0
    unscreened: tuple[str, ...] = ()
    completeness: str | None = None
    missing: str | None = None


@dataclass(slots=True)
class Budget:
    steps: int = MAX_STEPS
    seconds: float = MAX_SECONDS
    spend: int = MAX_SPEND
    spent: int = field(default=0)


class StepFailedError(RuntimeError):
    """The Brain could not answer a call; nothing of it is trusted."""


# One Brain operation of the task, its request, and the seconds left for it, answered with its value, the call's usage
# counts, and the model that answered.
Call = Callable[[str, Mapping[str, object], float], tuple[object, Mapping[str, int], str]]
# A model's input and output price in cents per million tokens; None names the dearest model the provider offers.
Price = Callable[[str | None], tuple[int, int]]
# A fresh Driver session on the same Browser in place of the failed one, opened by the task's absolute deadline; raises
# CuaError when the failed one could not be proven ended or the new one did not open.
Reopen = Callable[[cua.CuaSession, float], cua.CuaSession]
# The task's downloads so far in the protocol's form, read by the task's absolute deadline; None when unreadable.
Downloads = Callable[[float], Mapping[str, object] | None]


@dataclass(slots=True)
class Task:
    """One task's loop; `intact` says whether the Browser still runs on the egress process it started on.

    Without `reopen`, a failed Driver session ends the task.
    """

    objective: str
    language: str
    session: cua.CuaSession
    call: Call
    price: Price
    cancelled: Callable[[], bool]
    intact: Callable[[], bool]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    budget: Budget = field(default_factory=Budget)
    reopen: Reopen | None = None
    downloads: Downloads | None = None
    recovered: int = 0
    thread: browser_thread.Thread = field(default_factory=browser_thread.Thread)
    screen: screen.Screen = field(default_factory=screen.Screen)
    screenshot_at: int = -SCREENSHOT_EVERY
    wants_screenshot: bool = False
    model: str | None = None
    last_good: str | None = None
    section: str = ""
    returned: dict[str, int] = field(default_factory=lambda: {"unscreened": 0, "incomplete": 0})
    deadline: float = 0.0

    def run(self) -> Outcome:
        """Run the task to its end, by the absolute deadline it was given or one its budget fixes now."""
        self.deadline = self.deadline or self.clock() + self.budget.seconds
        self.session.deadline = self.deadline
        for index in range(self.budget.steps):
            ended = self._halted(index) or self._turn(index)
            if ended is not None:
                return replace(ended, recovered=self.recovered)
        return replace(self._final(self.budget.steps, None), recovered=self.recovered)

    # Proof that the task may go on

    def _halted(self, steps: int) -> Outcome | None:
        """Why the task makes no further call or action: Stop, no time left, or a Browser no longer intact."""
        if self.cancelled():
            return Outcome("stopped", steps=steps)
        if self.clock() >= self.deadline:
            return Outcome("budget", steps=steps)
        if not self.intact():
            return Outcome("failed", reason="browser-lost", steps=steps, cause="runtime/not-intact")
        return None

    def _may_act(self) -> bool:
        """Whether a read may send its next Driver command: the task may go on and the soft cutoff has not passed."""
        return self._halted(0) is None and not self._finishing()

    def _finishing(self) -> bool:
        return self.clock() >= self.deadline - FINISH_SECONDS

    def _affords(self, reserve: int, *, finishing: bool) -> bool:
        share = 100 if finishing else 100 - FINISH_SHARE
        return self.budget.spent + reserve <= self.budget.spend * share // 100

    # Brain calls

    def _bound(self, request: Mapping[str, object]) -> int:
        """The most input tokens `request` can cost: its rendered text's bytes, its screenshot, and the fixed prompt."""
        text = browser_protocol.request_text_bytes(
            str(request["objective"]), request["thread"], str(request["observation"]), request["downloads"]
        )
        return text + (IMAGE_TOKENS if request["screenshot"] is not None else 0) + PROMPT_TOKENS

    def _attempt(self, bound: int) -> int:
        """One provider attempt's worst cost: its input bound and the most output a call may write."""
        price_in, price_out = self.price(self.model)
        return bound * price_in + browser_protocol.MAX_CALL_OUTPUT_TOKENS * price_out

    def _ask(
        self, operation: str, request: Mapping[str, object], *, finishing: bool, bound: int | None = None
    ) -> object:
        """One Brain call under the spend ceiling; StepFailedError when it may not run or the Brain could not answer.

        `bound` is the call's input bound when its request is no step request.
        """
        attempt = self._attempt(self._bound(request) if bound is None else bound)
        reserve = attempt * browser_protocol.MAX_CALL_ATTEMPTS
        if not self._affords(reserve, finishing=finishing) or self._halted(0) is not None:
            raise StepFailedError("no budget")
        until = self.deadline if finishing else self.deadline - FINISH_SECONDS
        left = until - self.clock()
        if left <= 0:
            raise StepFailedError("no time")
        try:
            value, counts, model = self.call(operation, request, left)
        except StepFailedError:
            self.budget.spent += reserve
            raise
        self.model = model
        self.budget.spent += self._settled(counts, attempt)
        return value

    def _settled(self, counts: Mapping[str, int], attempt: int) -> int:
        """A call's reported usage at its model's price, and every attempt that reported none at its worst case."""
        price_in, price_out = self.price(self.model)
        reported = counts["model_calls"] - counts["failed_calls"] - counts["unreported_calls"]
        unaccounted = max(0, counts["provider_requests"] - reported)
        return counts["input_tokens"] * price_in + counts["output_tokens"] * price_out + unaccounted * attempt

    def _request(self, observation: cua.Observation, downloads: Mapping[str, object] | None) -> dict[str, object]:
        screenshot = base64.b64encode(observation.screenshot).decode() if observation.screenshot else None
        return {
            "objective": self.objective,
            "language": self.language,
            "thread": self.thread.wire(),
            "observation": observation.text,
            "screenshot": screenshot,
            "downloads": downloads,
        }

    # One turn

    def _turn(self, index: int) -> Outcome | None:
        """Observe, ask for one step, and act on it; the outcome that ends the task, or None to continue."""
        if self._finishing():
            return self._final(index, None)
        observation = self._observe(index)
        if isinstance(observation, Outcome) or observation is None:
            return observation
        downloads = None if self.downloads is None else self.downloads(self.deadline)
        request = self._request(observation, downloads)
        decision = self._decide(index, request)
        if isinstance(decision, Outcome):
            return decision
        if decision["kind"] == "done":
            return self._finish(index, request, observation, downloads, str(decision["summary"]))
        if decision["kind"] == "needs_person":
            return Outcome(
                "needs-person", reason=str(decision["reason"]), sentence=str(decision["sentence"]), steps=index + 1
            )
        return self._act(index, request, decision, observation, downloads)

    def _observe(self, index: int) -> cua.Observation | Outcome | None:
        """The current page, back on the last page that loaded after an error page, with any read pages joined."""
        notice = ""
        try:
            observation = self.session.observe(screenshot=self.wants_screenshot)
            if observation.url.startswith("chrome-error://") and self.last_good is not None:
                title = observation.title
                self.session.navigate(self.last_good)
                observation = self.session.observe(screenshot=self.wants_screenshot)
                notice = f"The last page you opened ({title}) did not load; you are back on {self.last_good}.\n"
        except cua.CuaError as exc:
            # Nothing was done on the page: the next step looks at it again on the fresh session.
            return self._recover(index, f"observe/{exc.code}")
        if observation.url.startswith(("https://", "http://")):
            self.last_good = observation.url
        if self.wants_screenshot:
            self.screenshot_at, self.wants_screenshot = index, False
        observation = read.shown(observation, notice, self.section)
        self.section = ""
        self.screen.observed(observation.url, observation.text)
        return observation

    def _decide(self, index: int, request: Mapping[str, object]) -> Outcome | dict[str, object]:
        """The step the Brain chose, admitted only while the task may still act on it, or how the task ends.

        Even a final step counts only if the task could still have acted on it: no Stop, time left, and the same
        egress process. A step the normal spend share cannot reserve finishes the task instead.
        """
        reserve = self._attempt(self._bound(request)) * browser_protocol.MAX_CALL_ATTEMPTS
        if not self._affords(reserve, finishing=False):
            return self._final(index, request)
        try:
            raw = self._ask("browser-step", request, finishing=False)
        except StepFailedError:
            # A model answers outside its form now and then: the step is asked once more, under its own reservation,
            # unless the normal spend share can no longer reserve it, when the task finishes instead.
            if self._halted(index) is not None or self._finishing():
                return self._unanswered(index, request)
            if not self._affords(reserve, finishing=False):
                return self._final(index, request)
            try:
                raw = self._ask("browser-step", request, finishing=False)
            except StepFailedError:
                return self._unanswered(index, request)
        # The soft cutoff passed while the Brain answered: no step is taken past it.
        ended = self._halted(index + 1) or (self._final(index + 1, request) if self._finishing() else None)
        if ended is not None:
            return ended
        decision = browser_protocol.canonical_step(raw)
        return Outcome("failed", reason="step-invalid", steps=index + 1) if decision is None else decision

    def _unanswered(self, index: int, request: Mapping[str, object]) -> Outcome:
        """How a step the Brain did not answer ends the task.

        Stop aborts the Brain request in flight, which then fails: that task was stopped, not failed. A step cut short
        by the soft cutoff finishes the task from its thread.
        """
        if (ended := self._halted(index)) is not None:
            return ended
        if self._finishing():
            return self._final(index, request)
        return Outcome("failed", reason="model-unavailable", steps=index)

    def _act(
        self,
        index: int,
        request: Mapping[str, object],
        decision: dict[str, object],
        observation: cua.Observation,
        downloads: Mapping[str, object] | None,
    ) -> Outcome | None:
        """Run one admitted page step and record its outcome; the outcome that ends the task, or None."""
        failure = None
        if decision["kind"] == "screenshot":
            self.wants_screenshot = index - self.screenshot_at >= SCREENSHOT_EVERY
            outcome = "ok" if self.wants_screenshot else "refused"
        else:
            try:
                outcome = self._perform(decision, observation)
            except read.UnstartedError as exc:
                # The read never clicked: nothing was done on the page.
                outcome, failure = "refused", f"act/{exc.code}"
            except cua.CuaError as exc:
                outcome, failure = "interrupted", f"act/{exc.code}"
        self._record(request, decision, outcome, observation, downloads)
        return None if failure is None else self._recover(index + 1, failure)

    def _perform(self, decision: dict[str, object], observation: cua.Observation) -> str:
        if decision["kind"] != "read":
            return _execute(self.session, decision, observation, self.sleep)
        return read.run(
            self.session, decision, observation, self._may_act, self.sleep, self.screen.observed, self._keep
        )

    def _keep(self, section: str) -> None:
        self.section = section

    def _record(
        self,
        request: Mapping[str, object],
        step: dict[str, object],
        outcome: str,
        observation: cua.Observation,
        downloads: Mapping[str, object] | None,
    ) -> None:
        """Append a step as its outcome arrives, folding the oldest steps first when it would not fit."""
        entry = {
            "step": step,
            "outcome": outcome,
            "page": {"url": observation.url, "title": observation.title},
            "text": observation.text,
            "downloads": downloads,
            "screenshot": observation.screenshot is not None,
        }
        count = self.thread.to_fold(browser_protocol.entry_bytes(entry))
        if count:
            first, last = self.thread.number(0), self.thread.number(count - 1)
            self.thread.fold(count, self._memory(request, first, last))
        self.thread.append(entry)

    def _memory(self, request: Mapping[str, object], first: int, last: int) -> str | None:
        """The agent's whole rewritten memory as steps leave its view; None when it could not be written."""
        try:
            value = self._ask("browser-fold", {**request, "first": first, "last": last}, finishing=False)
        except StepFailedError:
            return None
        memory = value.get("memory") if isinstance(value, dict) and set(value) == {"memory"} else None
        return browser_protocol.canonical_memory(memory)

    # Finishing

    def _finish(
        self,
        index: int,
        request: Mapping[str, object],
        observation: cua.Observation,
        downloads: Mapping[str, object] | None,
        summary: str,
    ) -> Outcome | None:
        """A done answer stands once screened and checked, or goes back to the agent with what it still needs."""
        unscreened = self.screen.unscreened(summary)
        if unscreened and self.returned["unscreened"] < MAX_RETURNS_UNSCREENED:
            self.returned["unscreened"] += 1
            return self._return(request, observation, downloads, UNSCREENED_NOTE + ", ".join(unscreened))
        verdict = self._check(summary)
        ended = self._halted(index + 1)
        if ended is not None:
            return ended
        if verdict.get("complete") is False and self.returned["incomplete"] < MAX_RETURNS_INCOMPLETE:
            self.returned["incomplete"] += 1
            return self._return(request, observation, downloads, INCOMPLETE_NOTE + str(verdict["missing"]))
        return _done(summary, index + 1, unscreened, verdict)

    def _return(
        self,
        request: Mapping[str, object],
        observation: cua.Observation,
        downloads: Mapping[str, object] | None,
        note: str,
    ) -> None:
        """Give a done answer back to the agent: a `returned` record whose note says what it still needs."""
        step = {"kind": "wait", "note": note[: browser_protocol.MAX_NOTE_CHARS]}
        self._record(request, step, "returned", observation, downloads)

    def _check(self, summary: str) -> dict[str, object]:
        """The completeness check's verdict, or not checked when it could not run or answered outside its form."""
        request = {"objective": self.objective, "language": self.language, "summary": summary}
        bound = sum(len(str(value).encode()) for value in request.values()) + PROMPT_TOKENS
        try:
            value = self._ask("browser-check", request, finishing=True, bound=bound)
        except StepFailedError:
            return {"complete": None, "missing": None}
        return browser_protocol.canonical_check(value) or {"complete": None, "missing": None}

    def _final(self, index: int, request: Mapping[str, object] | None) -> Outcome:
        """Past the soft cutoff: the Brain answers from the thread once, never another step; else the budget ends it."""
        if request is None:
            observation = self._observe(index)
            if not isinstance(observation, cua.Observation):
                return observation if isinstance(observation, Outcome) else Outcome("budget", steps=index)
            downloads = None if self.downloads is None else self.downloads(self.deadline)
            request = self._request(observation, downloads)
        try:
            value = self._ask("browser-final", request, finishing=True)
        except StepFailedError:
            return self._halted(index) or Outcome("budget", steps=index)
        ended = self._halted(index)
        if ended is not None:
            return ended
        summary = value.get("summary") if isinstance(value, dict) and set(value) == {"summary"} else None
        if summary is None or browser_protocol.canonical_step({"kind": "done", "summary": summary}) is None:
            return Outcome("budget", steps=index)
        return _done(summary, index, self.screen.unscreened(summary), {"complete": None, "missing": None})

    # Recovery

    def _recover(self, steps: int, cause: str) -> Outcome | None:
        """Replace the failed Driver session while the task may continue; how the task ends when it cannot."""
        # Stop, the deadline, and a lost egress win over the failure itself.
        ended = self._halted(steps)
        if ended is not None:
            return ended
        lost = Outcome("failed", reason="browser-lost", steps=steps, cause=cause)
        if self.reopen is None or self.recovered >= MAX_RECOVERIES:
            return lost
        try:
            self.session = self.reopen(self.session, self.deadline)
        except cua.CuaError as exc:
            # Stop and the deadline win over a recovery they cut short.
            return self._halted(steps) or replace(lost, cause=f"{cause}/reopen/{exc.code}")
        self.recovered += 1
        return None


def _done(summary: str, steps: int, unscreened: list[str], verdict: Mapping[str, object]) -> Outcome:
    """A done outcome with what screening and the completeness check left unresolved."""
    complete = verdict.get("complete")
    completeness = "not-checked" if complete is None else "complete" if complete else "incomplete"
    missing = str(verdict["missing"]) if completeness == "incomplete" else None
    return Outcome(
        "done",
        summary=summary,
        steps=steps,
        unscreened=tuple(unscreened),
        completeness=completeness,
        missing=missing,
    )


def _execute(
    session: cua.CuaSession,
    decision: Mapping[str, object],
    observation: cua.Observation,
    sleep: Callable[[float], None],
) -> str:
    """Execute one page step; `refused` when its ref was not offered for it or the page refused it."""
    kind = decision["kind"]
    needed = REF_ACTIONS.get(str(kind))
    if needed is not None and needed not in observation.refs.get(str(decision["ref"]), frozenset()):
        return "refused"
    try:
        if kind == "navigate":
            session.navigate(str(decision["url"]))
        elif kind == "click":
            session.click(str(decision["ref"]))
        elif kind == "type":
            session.type(str(decision["ref"]), str(decision["text"]))
        elif kind == "scroll":
            session.scroll(str(decision["direction"]))
        else:
            sleep(WAIT_SECONDS)
    except cua.CuaError as exc:
        if exc.fatal:
            raise
        return "navigation-failed" if exc.code == "navigation-failed" else "refused"
    return "ok"
