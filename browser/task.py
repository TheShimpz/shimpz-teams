"""One Browser task: Team's loop between the Browser agent's steps and the Cua Driver (ADR-0117 §5).

Each step sends the Brain operation `browser-step` the objective, the language, the bounded history, and the current
bounded observation, admits exactly one step from the closed set, checks a targeted ref against the observation it
was chosen from, executes it, and loops, until `done`, `needs_person`, Stop, a budget, or a failure ends the task.
Before every Brain call and every page action the task proves it may continue: no Stop, time and a step's token reserve
left, and the Browser still on the egress process it started on. No call outlives the task's deadline.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from browser import cua
from protocol.http.v1 import browser as browser_protocol

MAX_STEPS = 40
MAX_SECONDS = 600.0
# Input tokens the Brain may spend for one task, across its steps; about US$0.25 on the pinned Browser models.
MAX_INPUT_TOKENS = 400_000
# A step is asked only while its own request's bound remains: no token encodes less than one UTF-8 byte of its text, a
# screenshot of the Browser's fixed 1280x800 window costs at most IMAGE_TOKENS on either provider, and the Brain's fixed
# system prompt and step schema at most PROMPT_TOKENS.
IMAGE_TOKENS = 3_000
PROMPT_TOKENS = 4_000
SCREENSHOT_EVERY = 3
WAIT_SECONDS = 2.0
# Which observed action a step needs on the ref it names.
REF_ACTIONS = {"click": "click", "type": "type"}


@dataclass(frozen=True, slots=True)
class Outcome:
    """How a task ended: `done`, `needs-person`, `stopped`, `budget`, or `failed`, with its closed details."""

    status: str
    summary: str | None = None
    reason: str | None = None
    sentence: str | None = None
    steps: int = 0


@dataclass(slots=True)
class Budget:
    steps: int = MAX_STEPS
    seconds: float = MAX_SECONDS
    input_tokens: int = MAX_INPUT_TOKENS
    spent: int = field(default=0)


class StepFailedError(RuntimeError):
    """The Brain could not answer a step; the task fails without another attempt."""


# One `browser-step` request and the seconds left for it, answered with the raw step and its input tokens.
Step = Callable[[Mapping[str, object], float], tuple[object, int]]


@dataclass(slots=True)
class Task:
    """One task's loop; `intact` says whether the Browser still runs on the egress process it started on."""

    objective: str
    language: str
    session: cua.CuaSession
    step: Step
    cancelled: Callable[[], bool]
    intact: Callable[[], bool]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    budget: Budget = field(default_factory=Budget)
    history: list[dict[str, object]] = field(default_factory=list)
    screenshot_at: int = -SCREENSHOT_EVERY
    wants_screenshot: bool = False

    def run(self) -> Outcome:
        """Run the task to its end."""
        deadline = self.clock() + self.budget.seconds
        self.session.deadline = deadline
        for index in range(self.budget.steps):
            ended = self._halted(index, deadline, 0) or self._turn(index, deadline)
            if ended is not None:
                return ended
        return Outcome("budget", steps=self.budget.steps)

    def _halted(self, steps: int, deadline: float, reserve: int) -> Outcome | None:
        """Why the task makes no further call: Stop, no time or `reserve` tokens left, or a Browser no longer intact."""
        if self.cancelled():
            return Outcome("stopped", steps=steps)
        if self.clock() >= deadline or self.budget.spent + reserve > self.budget.input_tokens:
            return Outcome("budget", steps=steps)
        if not self.intact():
            return Outcome("failed", reason="browser-lost", steps=steps)
        return None

    def _turn(self, index: int, deadline: float) -> Outcome | None:
        """Observe, ask for one step, and act on it; the outcome that ends the task, or None to continue."""
        try:
            observation = self.session.observe(screenshot=self.wants_screenshot)
        except cua.CuaError:
            return Outcome("failed", reason="browser-lost", steps=index)
        if self.wants_screenshot:
            self.screenshot_at, self.wants_screenshot = index, False
        decision = self._decide(index, deadline, observation)
        if isinstance(decision, Outcome):
            return decision
        return self._act(index, decision, observation)

    def _decide(self, index: int, deadline: float, observation: cua.Observation) -> Outcome | dict[str, object]:
        """The page step the Brain chose, admitted only while the task may still act on it, or how the task ends.

        Even a final step counts only if the task could still have acted on it: no Stop, time and budget left, and the
        same egress process.
        """
        request = self._request(observation)
        ended = self._halted(index, deadline, _input_bound(request))
        if ended is not None:
            return ended
        try:
            raw, tokens = self.step(request, deadline - self.clock())
        except StepFailedError:
            # Stop aborts the Brain request in flight, which then fails: that task was stopped, not failed.
            if self.cancelled():
                return Outcome("stopped", steps=index)
            return Outcome("failed", reason="model-unavailable", steps=index)
        self.budget.spent += max(0, tokens)
        ended = self._halted(index + 1, deadline, 0)
        if ended is not None:
            return ended
        decision = browser_protocol.canonical_step(raw)
        if decision is None or decision["kind"] in {"done", "needs_person"}:
            return _terminal(decision, index + 1)
        return decision

    def _act(self, index: int, decision: dict[str, object], observation: cua.Observation) -> Outcome | None:
        """Run one admitted page step and record its outcome; the outcome that ends the task, or None."""
        if decision["kind"] == "screenshot":
            self.wants_screenshot = index - self.screenshot_at >= SCREENSHOT_EVERY
            outcome = "ok" if self.wants_screenshot else "refused"
        else:
            try:
                outcome = _execute(self.session, decision, observation, self.sleep)
            except cua.CuaError:
                return Outcome("failed", reason="browser-lost", steps=index + 1)
        self.history.append({"step": decision, "outcome": outcome})
        return None

    def _request(self, observation: cua.Observation) -> dict[str, object]:
        screenshot = base64.b64encode(observation.screenshot).decode() if observation.screenshot else None
        return {
            "objective": self.objective,
            "language": self.language,
            "history": self.history[-browser_protocol.MAX_HISTORY :],
            "observation": observation.text,
            "screenshot": screenshot,
        }


def _input_bound(request: Mapping[str, object]) -> int:
    """The most input tokens `request` can cost: its text's UTF-8 bytes, its one screenshot, and the fixed prompt."""
    text = json.dumps({**request, "screenshot": None}, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode()) + (IMAGE_TOKENS if request["screenshot"] is not None else 0) + PROMPT_TOKENS


def _terminal(decision: Mapping[str, object] | None, steps: int) -> Outcome:
    """The outcome a refused, `done`, or `needs_person` step ends the task with."""
    if decision is None:
        return Outcome("failed", reason="step-invalid", steps=steps)
    if decision["kind"] == "done":
        return Outcome("done", summary=str(decision["summary"]), steps=steps)
    return Outcome("needs-person", reason=str(decision["reason"]), sentence=str(decision["sentence"]), steps=steps)


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
        return "refused"
    return "ok"
