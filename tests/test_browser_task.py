"""One Browser task's loop: closed steps, refs checked against their observation, budgets, Stop, and failures."""

import unittest

from browser import cua, task

PNG = cua.PNG_SIGNATURE + b"pixels"


class FakeSession:
    def __init__(self, refs=None, *, fail_observe=False, refuse=(), fatal=(), unloaded=()):
        self.refs = refs if refs is not None else {"p1:0": frozenset({"type"}), "p1:1": frozenset({"click"})}
        self.fail_observe, self.refuse, self.fatal = fail_observe, set(refuse), set(fatal)
        self.unloaded = set(unloaded)
        self.calls, self.screenshots, self.observed = [], [], 0

    def observe(self, *, screenshot=False):
        self.observed += 1
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
        if name in self.unloaded:
            raise cua.CuaError("navigation-failed", fatal=False)

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
        # Each step is recorded with the page it was chosen on, so the agent never returns to recall it.
        self.assertEqual(
            requests[-1]["history"][-1],
            {"step": {"kind": "wait"}, "outcome": "ok", "page": {"url": "https://example.com", "title": "Example"}},
        )
        self.assertEqual(
            (requests[0]["objective"], requests[0]["language"], requests[0]["screenshot"]),
            ("Veja o prazo.", "pt-BR", None),
        )

    def test_a_navigation_that_did_not_load_is_an_outcome_the_agent_sees_after_looking_again(self):
        session = FakeSession(unloaded={"navigate"})
        step, requests = _steps(
            {"kind": "navigate", "url": "https://www.python.org"},
            {"kind": "done", "summary": "Não carregou."},
        )
        outcome, _ = _run(session, step)
        self.assertEqual(outcome, task.Outcome("done", summary="Não carregou.", steps=2))
        self.assertEqual(requests[-1]["history"][0]["outcome"], "navigation-failed")
        # The next step was chosen on a fresh observation, as after any step.
        self.assertEqual(len(session.screenshots), 2)

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
        # Each step is asked only while its own request's bound, at least the fixed prompt's, still fits the budget.
        spent = task.Budget(input_tokens=24_000)
        outcome, _ = _run(FakeSession(), _steps(wait, wait, done, tokens=10_000)[0], budget=spent)
        self.assertEqual(outcome, task.Outcome("budget", steps=2))
        outcome, _ = _run(FakeSession(), _steps(wait, wait)[0], budget=task.Budget(steps=2))
        self.assertEqual(outcome, task.Outcome("budget", steps=2))
        self.assertEqual(
            _run(FakeSession(fail_observe=True), _steps(done)[0])[0],
            task.Outcome("failed", reason="browser-lost", cause="observe/tool-failed"),
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
            task.Outcome("failed", reason="browser-lost", steps=1, cause="act/tool-failed"),
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
        self.assertEqual(outcome, task.Outcome("failed", reason="browser-lost", cause="runtime/not-intact"))
        session = FakeSession()
        lost = after(3)
        outcome, _ = _run(session, _steps(navigate)[0], intact=lambda: not lost())
        self.assertEqual(
            (outcome, session.calls),
            (task.Outcome("failed", reason="browser-lost", steps=1, cause="runtime/not-intact"), []),
        )

    def test_a_final_step_counts_only_if_the_task_could_still_act_on_it(self):
        done = {"kind": "done", "summary": "ok"}

        def after(calls):
            count = iter(range(1, 100))
            return lambda: next(count) >= calls

        self.assertEqual(_run(FakeSession(), _steps(done)[0], cancelled=after(3))[0], task.Outcome("stopped", steps=1))
        lost = after(3)
        self.assertEqual(
            _run(FakeSession(), _steps(done)[0], intact=lambda: not lost())[0],
            task.Outcome("failed", reason="browser-lost", steps=1, cause="runtime/not-intact"),
        )
        self.assertEqual(
            _run(FakeSession(), _steps(done, tokens=task.MAX_INPUT_TOKENS + 1)[0])[0],
            task.Outcome("budget", steps=1),
        )

    def test_a_requests_input_bound_counts_its_bytes_its_screenshot_and_the_prompt(self):
        request = {"objective": "é", "language": "pt-BR", "history": [], "observation": "x", "screenshot": None}
        text = '{"objective":"é","language":"pt-BR","history":[],"observation":"x","screenshot":null}'
        self.assertEqual(task._input_bound(request), len(text.encode()) + task.PROMPT_TOKENS)
        self.assertEqual(
            task._input_bound({**request, "screenshot": "AAAA"}),
            len(text.encode()) + task.PROMPT_TOKENS + task.IMAGE_TOKENS,
        )

    def test_a_failed_driver_session_is_replaced_and_the_task_continues_where_it_was(self):
        failed = FakeSession(fatal={"click"})
        fresh = FakeSession()
        reopened = []

        def reopen(session, deadline):
            reopened.append((session, deadline))
            return fresh

        step, requests = _steps(
            {"kind": "click", "ref": "p1:1"},
            {"kind": "scroll", "direction": "down"},
            {"kind": "done", "summary": "ok"},
        )
        outcome, _ = _run(failed, step, reopen=reopen)
        self.assertEqual(outcome, task.Outcome("done", summary="ok", steps=3, recovered=1))
        self.assertEqual(reopened, [(failed, task.MAX_SECONDS)])
        # The failed session is never used again, and the click whose effect is unknown is in the history as such.
        self.assertEqual((failed.calls, fresh.calls), ([("click", "p1:1")], [("scroll", "down")]))
        self.assertEqual([entry["outcome"] for entry in requests[-1]["history"]], ["interrupted", "ok"])

    def test_an_observation_that_failed_is_looked_at_again_on_a_fresh_session(self):
        fresh = FakeSession()
        step, requests = _steps({"kind": "done", "summary": "ok"})
        outcome, _ = _run(FakeSession(fail_observe=True), step, reopen=lambda _session, _deadline: fresh)
        self.assertEqual(outcome, task.Outcome("done", summary="ok", steps=2, recovered=1))
        self.assertEqual((requests[0]["history"], len(fresh.screenshots)), ([], 1))

    def test_recoveries_are_bounded_and_a_failed_one_ends_the_task_with_its_cause(self):
        outcome, _ = _run(
            FakeSession(fail_observe=True),
            _steps({"kind": "done", "summary": "ok"})[0],
            reopen=lambda _session, _deadline: FakeSession(fail_observe=True),
        )
        self.assertEqual(
            outcome,
            task.Outcome(
                "failed", reason="browser-lost", steps=task.MAX_RECOVERIES, cause="observe/tool-failed", recovered=3
            ),
        )

        def refused(_session, _deadline):
            raise cua.CuaError("driver-not-ended")

        outcome, _ = _run(FakeSession(fail_observe=True), _steps()[0], reopen=refused)
        self.assertEqual(
            outcome,
            task.Outcome("failed", reason="browser-lost", cause="observe/tool-failed/reopen/driver-not-ended"),
        )

    def test_stop_and_a_lost_egress_win_over_a_recovery(self):
        reopened = []

        def reopen(_session, _deadline):
            reopened.append(True)
            raise cua.CuaError("driver-unavailable")

        # Stop came while the session was being replaced.
        stopped = iter([False, False, True])
        outcome, _ = _run(FakeSession(fail_observe=True), _steps()[0], reopen=reopen, cancelled=lambda: next(stopped))
        self.assertEqual((outcome, reopened), (task.Outcome("stopped"), [True]))
        # Stop during the failure after the last allowed recovery still ends the task stopped, not lost.
        sessions = []

        def failing(_session, _deadline):
            sessions.append(FakeSession(fail_observe=True))
            return sessions[-1]

        outcome, _ = _run(
            FakeSession(fail_observe=True),
            _steps()[0],
            reopen=failing,
            cancelled=lambda: len(sessions) == task.MAX_RECOVERIES and sessions[-1].observed > 0,
        )
        self.assertEqual(outcome, task.Outcome("stopped", steps=task.MAX_RECOVERIES, recovered=task.MAX_RECOVERIES))
        # The egress was lost before the session failed: no session is replaced.
        lost = iter([True, False])
        outcome, _ = _run(FakeSession(fail_observe=True), _steps()[0], reopen=reopen, intact=lambda: next(lost))
        self.assertEqual(outcome, task.Outcome("failed", reason="browser-lost", cause="runtime/not-intact"))
        self.assertEqual(reopened, [True])

    def test_no_call_outlives_the_task_deadline(self):
        seconds = []
        ticks = iter([10.0, 10.0, 10.0, 25.0, 25.0, 30.0, 30.0, 30.0, 30.0])
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
