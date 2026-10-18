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


def _steps(*steps, tokens=1000):
    requests = []
    answers = iter(steps)

    def step(request):
        requests.append(request)
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
        spent = task.Budget(input_tokens=1500)
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


if __name__ == "__main__":
    unittest.main()
