"""One Browser task's loop: closed steps, refs checked against their observation, budgets, Stop, and failures."""

import unittest

from browser import cua, task

PNG = cua.PNG_SIGNATURE + b"pixels"


class FakeSession:
    def __init__(self, refs=None, *, fail_observe=False, refuse=(), fatal=()):
        self.refs = refs if refs is not None else {"p1:0": frozenset({"type"}), "p1:1": frozenset({"click"})}
        self.fail_observe, self.refuse, self.fatal = fail_observe, set(refuse), set(fatal)
        self.calls, self.screenshots = [], []

    def observe(self, *, screenshot=False):
        if self.fail_observe:
            raise cua.CuaError("tool-failed")
        self.screenshots.append(screenshot)
        return cua.Observation(
            "https://example.com", "Example", "url: https://example.com", self.refs, PNG if screenshot else None
        )

    def _act(self, name, *arguments):
        self.calls.append((name, *arguments))
        if name in self.fatal:
            raise cua.CuaError("tool-failed")
        if name in self.refuse:
            raise cua.CuaError("action-refused", fatal=False)

    def navigate(self, url):
        self._act("navigate", url)

    def click(self, ref):
        self._act("click", ref)

    def type(self, ref, text):
        self._act("type", ref, text)

    def scroll(self, direction):
        self._act("scroll", direction)


def _steps(*steps, tokens=1000, seconds=None):
    requests = []
    answers = iter(steps)

    def step(request, left):
        requests.append(request)
        if seconds is not None:
            seconds.append(left)
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer, tokens

    return step, requests


def _run(session, step, **options):
    sleeps = []
    clock = options.pop("clock", lambda: 0.0)
    outcome = task.Task(
        "Veja o prazo.",
        "pt-BR",
        session,
        step,
        options.pop("cancelled", lambda: False),
        options.pop("intact", lambda: True),
        clock=clock,
        sleep=sleeps.append,
        **options,
    ).run()
    return outcome, sleeps


class BrowserTaskTests(unittest.TestCase):
    def test_a_task_executes_each_closed_step_then_ends_with_the_agents_summary(self):
        session = FakeSession()
        step, requests = _steps(
            {"kind": "navigate", "url": "https://example.com/editais"},
            {"kind": "type", "ref": "p1:0", "text": "ia"},
            {"kind": "click", "ref": "p1:1"},
            {"kind": "scroll", "direction": "down"},
            {"kind": "wait"},
            {"kind": "done", "summary": "Prazo: 30/11."},
        )
        outcome, sleeps = _run(session, step)
        self.assertEqual(outcome, task.Outcome("done", summary="Prazo: 30/11.", steps=6))
        self.assertEqual(
            session.calls,
            [
                ("navigate", "https://example.com/editais"),
                ("type", "p1:0", "ia"),
                ("click", "p1:1"),
                ("scroll", "down"),
            ],
        )
        self.assertEqual(sleeps, [task.WAIT_SECONDS])
        self.assertEqual(requests[0]["history"], [])
        self.assertEqual(requests[-1]["history"][-1], {"step": {"kind": "wait"}, "outcome": "ok"})
        self.assertEqual(
            (requests[0]["objective"], requests[0]["language"], requests[0]["screenshot"]),
            ("Veja o prazo.", "pt-BR", None),
        )

    def test_a_ref_not_offered_for_its_action_and_a_refused_page_action_are_outcomes_not_failures(self):
        session = FakeSession(refuse={"navigate"})
        step, requests = _steps(
            {"kind": "click", "ref": "p1:0"},
            {"kind": "type", "ref": "p9:9", "text": "x"},
            {"kind": "navigate", "url": "https://example.com"},
            {"kind": "needs_person", "reason": "sign-in", "sentence": "O site pede login."},
        )
        outcome, _ = _run(session, step)
        self.assertEqual(
            outcome, task.Outcome("needs-person", reason="sign-in", sentence="O site pede login.", steps=4)
        )
        self.assertEqual([entry["outcome"] for entry in requests[-1]["history"]], ["refused", "refused", "refused"])
        self.assertEqual(session.calls, [("navigate", "https://example.com")])

    def test_screenshots_are_rationed_to_one_every_three_steps(self):
        session = FakeSession()
        step, requests = _steps(
            {"kind": "screenshot"},
            {"kind": "screenshot"},
            {"kind": "wait"},
            {"kind": "wait"},
            {"kind": "screenshot"},
            {"kind": "done", "summary": "ok"},
        )
        _run(session, step)
        self.assertEqual(session.screenshots, [False, True, False, False, False, True])
        self.assertIsNotNone(requests[1]["screenshot"])
        self.assertEqual([entry["outcome"] for entry in requests[-1]["history"]], ["ok", "refused", "ok", "ok", "ok"])

    def test_stop_budgets_and_failures_end_the_task_with_their_closed_reason(self):
        done = {"kind": "done", "summary": "ok"}
        wait = {"kind": "wait"}
        self.assertEqual(_run(FakeSession(), _steps(done)[0], cancelled=lambda: True)[0], task.Outcome("stopped"))
        ticks = iter([0.0, task.MAX_SECONDS])
        self.assertEqual(_run(FakeSession(), _steps(done)[0], clock=lambda: next(ticks))[0], task.Outcome("budget"))
        spent = task.Budget(input_tokens=task.STEP_INPUT_RESERVE + 1500)
        outcome, _ = _run(FakeSession(), _steps(wait, wait, done)[0], budget=spent)
        self.assertEqual(outcome, task.Outcome("budget", steps=2))
        outcome, _ = _run(FakeSession(), _steps(wait, wait)[0], budget=task.Budget(steps=2))
        self.assertEqual(outcome, task.Outcome("budget", steps=2))
        self.assertEqual(
            _run(FakeSession(fail_observe=True), _steps(done)[0])[0], task.Outcome("failed", reason="browser-lost")
        )
        self.assertEqual(
            _run(FakeSession(), _steps(task.StepFailedError("down"))[0])[0],
            task.Outcome("failed", reason="model-unavailable"),
        )
        self.assertEqual(
            _run(FakeSession(), _steps({"kind": "run_script"})[0])[0],
            task.Outcome("failed", reason="step-invalid", steps=1),
        )
        self.assertEqual(
            _run(FakeSession(fatal={"click"}), _steps({"kind": "click", "ref": "p1:1"})[0])[0],
            task.Outcome("failed", reason="browser-lost", steps=1),
        )

    def test_every_call_and_action_waits_for_proof_that_the_task_may_continue(self):
        navigate = {"kind": "navigate", "url": "https://example.com"}

        def after(calls):
            count = iter(range(1, 100))
            return lambda: next(count) >= calls

        # Stop after the Brain answered: its step never reaches the page.
        session = FakeSession()
        outcome, _ = _run(session, _steps(navigate)[0], cancelled=after(3))
        self.assertEqual((outcome, session.calls), (task.Outcome("stopped", steps=1), []))
        # Stop while the page was observed: no Brain call is made.
        step, requests = _steps(navigate)
        outcome, _ = _run(FakeSession(), step, cancelled=after(2))
        self.assertEqual((outcome, requests), (task.Outcome("stopped"), []))
        # Stop aborts the Brain request in flight: the task was stopped, not failed.
        outcome, _ = _run(FakeSession(), _steps(task.StepFailedError("aborted"))[0], cancelled=after(3))
        self.assertEqual(outcome, task.Outcome("stopped"))
        # The deadline passed while the Brain answered, or the step spent more than the budget left.
        ticks = iter([0.0, 0.0, 0.0, 0.0, task.MAX_SECONDS])
        session = FakeSession()
        outcome, _ = _run(session, _steps(navigate)[0], clock=lambda: next(ticks))
        self.assertEqual((outcome, session.calls), (task.Outcome("budget", steps=1), []))
        session = FakeSession()
        outcome, _ = _run(session, _steps(navigate, tokens=task.MAX_INPUT_TOKENS + 1)[0])
        self.assertEqual((outcome, session.calls), (task.Outcome("budget", steps=1), []))
        # The Browser left the egress process it started on.
        outcome, _ = _run(FakeSession(), _steps(navigate)[0], intact=lambda: False)
        self.assertEqual(outcome, task.Outcome("failed", reason="browser-lost"))
        session = FakeSession()
        lost = after(3)
        outcome, _ = _run(session, _steps(navigate)[0], intact=lambda: not lost())
        self.assertEqual((outcome, session.calls), (task.Outcome("failed", reason="browser-lost", steps=1), []))

    def test_no_call_outlives_the_task_deadline(self):
        seconds = []
        ticks = iter([10.0, 10.0, 10.0, 25.0, 25.0, 30.0, 30.0, 30.0])
        session = FakeSession()
        _run(
            session,
            _steps({"kind": "wait"}, {"kind": "done", "summary": "ok"}, seconds=seconds)[0],
            clock=lambda: next(ticks),
        )
        self.assertEqual(session.deadline, 10.0 + task.MAX_SECONDS)
        self.assertEqual(seconds, [task.MAX_SECONDS - 15.0, task.MAX_SECONDS - 20.0])


if __name__ == "__main__":
    unittest.main()
