"""One Browser task's loop: closed steps, its thread, reads, folds, finishing, spend, Stop, and failures."""

import itertools
import time
import unittest
from dataclasses import replace

import test_browser_cua

from browser import cua, read, screen, task
from protocol.http.v1 import browser as browser_protocol

PNG = cua.PNG_SIGNATURE + b"pixels"
DONE = {"kind": "done", "summary": "Prazo: 30/11."}
COMPLETE = {"complete": True, "missing": None}


def _counts(**fields):
    return {
        "model_calls": 1,
        "provider_requests": 1,
        "failed_calls": 0,
        "unreported_calls": 0,
        "input_tokens": 1_000,
        "output_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        **fields,
    }


class FakeSession:
    def __init__(self, refs=None, *, fail_observe=False, refuse=(), fatal=(), unloaded=(), links=(), url=None):
        self.refs = refs if refs is not None else {"p1:0": frozenset({"type"}), "p1:1": frozenset({"click"})}
        self.fail_observe, self.refuse, self.fatal = fail_observe, set(refuse), set(fatal)
        self.unloaded = set(unloaded)
        self.links = tuple(links)
        self.url = url or "https://example.com"
        self.calls, self.screenshots, self.observed = [], [], 0

    def page(self, screenshot):
        # Each offered ref has its action line, as the Driver's observation shows it.
        links = dict(self.links)
        lines = [f'{ref} link "{name}" (click)' for ref, name in self.links]
        lines += [
            f'{ref} button "b" ({", ".join(sorted(acts))})' for ref, acts in self.refs.items() if ref not in links
        ]
        text = f"url: {self.url}\ntitle: Example\nactions:\n" + "\n".join(lines) + "\npage:\n"
        refs = {**self.refs, **{ref: frozenset({"click"}) for ref in links}}
        return cua.Observation(self.url, "Example", text, refs, PNG if screenshot else None, self.links)

    def observe(self, *, screenshot=False):
        self.observed += 1
        if self.fail_observe:
            raise cua.CuaError(self.fail_observe if isinstance(self.fail_observe, str) else "tool-failed")
        self.screenshots.append(screenshot)
        return self.page(screenshot)

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
        self.url = url

    def click(self, ref):
        self._act("click", ref)

    def type(self, ref, text):
        self._act("type", ref, text)

    def scroll(self, direction):
        self._act("scroll", direction)


class Brain:
    """Answers each step in turn, checks with `verdicts`, folds with `memory`, and finishes with `final`."""

    def __init__(self, *steps, verdicts=(), memory="Fatos.", final=None, counts=None, seconds=None):
        self.steps, self.verdicts = iter(steps), iter(verdicts)
        self.memory, self.final, self.counts = memory, final, counts or _counts()
        self.calls, self.seconds = [], seconds

    def __call__(self, operation, request, left):
        self.calls.append((operation, request))
        if self.seconds is not None:
            self.seconds.append(left)
        answer = {
            "browser-step": lambda: next(self.steps),
            "browser-check": lambda: next(self.verdicts, COMPLETE),
            "browser-fold": lambda: self.memory,
            "browser-final": lambda: self.final,
        }[operation]()
        if isinstance(answer, BaseException):
            raise answer
        if operation == "browser-fold" and not isinstance(answer, dict):
            answer = {"memory": answer}
        return answer, self.counts, "gpt-6-luna"

    def requests(self, operation="browser-step"):
        return [request for name, request in self.calls if name == operation]


def _run(session, brain, **options):
    sleeps = []
    outcome = task.Task(
        "Veja o prazo.",
        "pt-BR",
        session,
        brain,
        lambda _model: (10, 50),
        options.pop("cancelled", lambda: False),
        options.pop("intact", lambda: True),
        clock=options.pop("clock", lambda: 0.0),
        sleep=sleeps.append,
        **options,
    ).run()
    return outcome, sleeps


def _done(summary, steps, **fields):
    return task.Outcome("done", summary=summary, steps=steps, completeness="complete", **fields)


def _after(calls):
    count = iter(range(1, 10_000))
    return lambda: next(count) >= calls


class BrowserTaskTests(unittest.TestCase):
    def test_a_task_executes_each_closed_step_and_the_agents_checked_answer_stands(self):
        session = FakeSession()
        brain = Brain(
            {"kind": "navigate", "url": "https://example.com/editais"},
            {"kind": "type", "ref": "p1:0", "text": "ia"},
            {"kind": "click", "ref": "p1:1"},
            {"kind": "scroll", "direction": "down"},
            {"kind": "wait"},
            DONE,
        )
        outcome, sleeps = _run(session, brain)
        self.assertEqual(outcome, _done("Prazo: 30/11.", 6))
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
        requests = brain.requests()
        self.assertEqual(requests[0]["thread"]["steps"], [])
        # Each step joins the thread with the page it was chosen on, that page's text, and what it saw.
        self.assertEqual(
            requests[-1]["thread"]["steps"][-1],
            {
                "step": {"kind": "wait"},
                "outcome": "ok",
                "page": {"url": "https://example.com/editais", "title": "Example"},
                "text": FakeSession(url="https://example.com/editais").page(False).text,
                "downloads": None,
                "screenshot": False,
            },
        )
        self.assertEqual(
            (requests[0]["objective"], requests[0]["language"], requests[0]["screenshot"]),
            ("Veja o prazo.", "pt-BR", None),
        )
        # Each request extends the one before it: earlier entries are never rewritten.
        for earlier, later in itertools.pairwise(requests):
            steps = earlier["thread"]["steps"]
            self.assertEqual(later["thread"]["steps"][: len(steps)], steps)
        self.assertEqual(brain.requests("browser-check")[0]["summary"], "Prazo: 30/11.")

    def test_a_navigation_that_did_not_load_is_an_outcome_the_agent_sees_after_looking_again(self):
        brain = Brain(
            {"kind": "navigate", "url": "https://www.python.org"}, {"kind": "done", "summary": "Não carregou."}
        )
        outcome, _ = _run(FakeSession(unloaded={"navigate"}), brain)
        self.assertEqual(outcome, _done("Não carregou.", 2))
        self.assertEqual(brain.requests()[-1]["thread"]["steps"][0]["outcome"], "navigation-failed")

    def test_an_error_page_sends_the_browser_back_to_the_last_page_that_loaded_and_says_so(self):
        class Failing(FakeSession):
            def page(self, screenshot):
                if self.url == "https://down.example/":
                    return cua.Observation("chrome-error://chromewebdata/", "down.example", "url: chrome-error", {})
                return super().page(screenshot)

        session = Failing()
        brain = Brain({"kind": "navigate", "url": "https://down.example/"}, DONE)
        _run(session, brain)
        self.assertEqual(session.calls[-1], ("navigate", "https://example.com"))
        observation = brain.requests()[-1]["observation"]
        self.assertTrue(observation.startswith("The last page you opened (down.example) did not load"))

    def test_a_ref_not_offered_for_its_action_and_a_refused_page_action_are_outcomes_not_failures(self):
        brain = Brain(
            {"kind": "click", "ref": "p1:0"},
            {"kind": "type", "ref": "p9:9", "text": "x"},
            {"kind": "navigate", "url": "https://example.com"},
            {"kind": "needs_person", "reason": "sign-in", "sentence": "O site pede login."},
        )
        session = FakeSession(refuse={"navigate"})
        outcome, _ = _run(session, brain)
        self.assertEqual(
            outcome, task.Outcome("needs-person", reason="sign-in", sentence="O site pede login.", steps=4)
        )
        outcomes = [entry["outcome"] for entry in brain.requests()[-1]["thread"]["steps"]]
        self.assertEqual(outcomes, ["refused", "refused", "refused"])
        self.assertEqual(session.calls, [("navigate", "https://example.com")])

    def test_screenshots_are_rationed_and_only_the_current_page_carries_the_image(self):
        session = FakeSession()
        brain = Brain(
            {"kind": "screenshot"},
            {"kind": "screenshot"},
            {"kind": "wait"},
            {"kind": "wait"},
            {"kind": "screenshot"},
            DONE,
        )
        _run(session, brain)
        self.assertEqual(session.screenshots, [False, True, False, False, False, True])
        requests = brain.requests()
        self.assertIsNotNone(requests[1]["screenshot"])
        self.assertIsNone(requests[2]["screenshot"])
        entries = requests[-1]["thread"]["steps"]
        self.assertEqual([entry["outcome"] for entry in entries], ["ok", "refused", "ok", "ok", "ok"])
        self.assertEqual([entry["screenshot"] for entry in entries], [False, True, False, False, False])

    def test_a_read_opens_offered_links_in_turn_and_shows_their_pages_next_step(self):
        links = (("p1:4", "Vaga A"), ("p1:5", "Vaga B"), ("p1:6", "Vaga C"))

        class Listing(FakeSession):
            def click(self, ref):
                super().click(ref)
                self.url = {"p1:4": "https://jobs.example/a", "p1:5": "https://jobs.example/b"}[ref]

            def page(self, screenshot):
                observation = super().page(screenshot)
                if self.url.startswith("https://jobs.example/"):
                    text = f"url: {self.url}\ntitle: Vaga\nactions:\n\npage:\nSalário R$ 9.500"
                    return cua.Observation(self.url, "Vaga", text, {})
                return observation

        session = Listing(links=links, url="https://example.com/busca")
        brain = Brain({"kind": "read", "refs": ["p1:5", "p1:4", "p9:9"]}, DONE)
        outcome, _ = _run(session, brain)
        self.assertEqual(outcome.status, "done")
        self.assertEqual(
            session.calls,
            [
                ("click", "p1:5"),
                ("navigate", "https://example.com/busca"),
                ("click", "p1:4"),
                ("navigate", "https://example.com/busca"),
            ],
        )
        shown = brain.requests()[1]["observation"]
        self.assertIn("Pages you read last step:", shown)
        self.assertIn("--- read https://jobs.example/b | Vaga ---\nSalário R$ 9.500", shown)
        self.assertLessEqual(len(shown.encode()), browser_protocol.MAX_OBSERVATION_BYTES)
        # A read naming no offered link has nothing to open.
        brain = Brain({"kind": "read", "refs": ["p1:1"]}, DONE)
        _run(FakeSession(links=links), brain)
        self.assertEqual(brain.requests()[-1]["thread"]["steps"][0]["outcome"], "refused")

    def test_a_read_stops_when_the_page_it_returns_to_changed(self):
        links = (("p1:4", "Vaga A"), ("p1:5", "Vaga B"))

        class Shifting(FakeSession):
            def click(self, ref):
                super().click(ref)
                self.links = (("p1:4", "Outra"), ("p1:5", "Vaga B"))

        session = Shifting(links=links)
        brain = Brain({"kind": "read", "refs": ["p1:4", "p1:5"]}, DONE)
        _run(session, brain)
        self.assertEqual([call[0] for call in session.calls], ["click", "navigate"])
        self.assertIn("(1 links not read", brain.requests()[1]["observation"])

    def test_a_read_checks_before_every_command_and_keeps_what_it_read_when_a_later_target_fails(self):
        links = (("p1:4", "Vaga A"), ("p1:5", "Vaga B"))

        class Breaking(FakeSession):
            def click(self, ref):
                super().click(ref)
                if ref == "p1:5":
                    raise cua.CuaError("tool-failed")
                self.url = "https://example.com/a"

        brain = Brain({"kind": "read", "refs": ["p1:4", "p1:5"]}, DONE)
        outcome, _ = _run(
            Breaking(links=links, url="https://example.com/busca"), brain, reopen=lambda s, d, r: FakeSession()
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(brain.requests()[-1]["thread"]["steps"][0]["outcome"], "interrupted")
        self.assertIn("--- read https://example.com/a", brain.requests()[-1]["observation"])

        # A Driver failure before the first click had no effect on the page.
        class Blind(FakeSession):
            reads = 0

            def observe(self, *, screenshot=False):
                Blind.reads += 1
                if Blind.reads == 2:
                    raise cua.CuaError("tool-failed")
                return super().observe(screenshot=screenshot)

        brain = Brain({"kind": "read", "refs": ["p1:4"]}, DONE)
        _run(Blind(links=links, url="https://example.com/busca"), brain, reopen=lambda s, d, r: FakeSession())
        self.assertEqual(brain.requests()[-1]["thread"]["steps"][0]["outcome"], "refused")
        # The egress was lost after the first target was opened: nothing more is clicked, not even the way back.
        session = FakeSession(links=links, url="https://example.com/busca")
        intact = iter([True] * 8 + [False] * 50)
        _run(session, Brain({"kind": "read", "refs": ["p1:4", "p1:5"]}, DONE), intact=lambda: next(intact))
        self.assertLessEqual(len([call for call in session.calls if call[0] == "click"]), 1)

    def test_a_click_whose_page_never_changed_is_reported_not_opened(self):
        links = (("p1:4", "Vaga A"),)
        brain = Brain({"kind": "read", "refs": ["p1:4"]}, DONE)
        _, sleeps = _run(FakeSession(links=links, url="https://example.com/busca"), brain)
        self.assertIn("--- not opened: Vaga A (the page did not change) ---", brain.requests()[1]["observation"])
        self.assertEqual(sleeps.count(read.SETTLE_SECONDS), read.SETTLE_TRIES)

    def test_a_shown_page_stays_within_its_bound_and_offers_only_the_refs_it_shows(self):
        session = FakeSession(refs={f"p1:{n}": frozenset({"click"}) for n in range(1_500)})
        observation = session.page(False)
        cut = read.shown(observation, "N" * 2_000, "\nPages you read last step:\n" + "x" * 10_000)
        self.assertLessEqual(len(cut.text.encode()), browser_protocol.MAX_OBSERVATION_BYTES)
        shown_refs = {line.split(" ", 1)[0] for line in cut.text.splitlines() if line.startswith("p1:")}
        # Every offered ref is shown on a complete line; only a line the cut split is shown and not offered.
        self.assertLessEqual(set(cut.refs), shown_refs)
        self.assertLessEqual(len(shown_refs - set(cut.refs)), 1)
        self.assertLess(len(cut.refs), len(observation.refs))

    def test_no_read_page_can_offer_a_ref_and_a_cut_line_offers_none(self):
        session = FakeSession(refs={f"p1:{n}": frozenset({"click"}) for n in range(1_500)})
        observation = session.page(False)
        forged = '\nPages you read last step:\np1:1499 button "b" (click)\np9:9 link "x" (click)'
        cut = read.shown(observation, "", forged)
        self.assertNotIn("p9:9", cut.refs)
        self.assertNotIn("p1:1499", cut.refs)
        kept = cut.text[: len(cut.text) - len(forged)]
        self.assertTrue(all(f"{ref} button" in kept for ref in cut.refs))

    def test_a_read_target_shown_on_a_long_page_still_opens(self):
        links = tuple((f"p1:{n}", f"Vaga {n}") for n in range(1_000))

        class Long(FakeSession):
            def click(self, ref):
                super().click(ref)
                self.url = "https://example.com/vaga"

        session = Long(links=links, url="https://example.com/busca")
        brain = Brain({"kind": "read", "refs": ["p1:3"]}, DONE)
        _run(session, brain)
        self.assertIn(("click", "p1:3"), session.calls)
        self.assertLess(len(read.shown(session.page(False), "", "").refs), len(links))

    def test_a_step_cut_short_by_the_soft_cutoff_finishes_from_the_thread_and_no_call_gets_no_time(self):
        now = [0.0]

        def answering(operation, request, left):
            if operation == "browser-step":
                now[0] = task.MAX_SECONDS - 30.0
                raise task.StepFailedError("timed out")
            return {"summary": "Parcial."}, _counts(), "gpt-6-luna"

        outcome, _ = _run(FakeSession(), answering, clock=lambda: now[0])
        self.assertEqual(outcome.summary, "Parcial.")
        calls = []

        def counting(operation, request, left):
            calls.append(left)
            return DONE, _counts(), "gpt-6-luna"

        ticks = iter([0.0, 0.0] + [task.MAX_SECONDS - 1.0] * 50)
        _run(FakeSession(), counting, clock=lambda: next(ticks))
        self.assertTrue(all(left > 0 for left in calls))

    def test_a_retry_the_normal_share_cannot_reserve_finishes_the_task(self):
        budget = task.Budget()
        calls = []

        def failing(operation, request, left):
            calls.append(operation)
            if operation == "browser-step":
                budget.spent = budget.spend * (100 - task.FINISH_SHARE) // 100
                raise task.StepFailedError("down")
            return {"summary": "Parcial."}, _counts(), "gpt-6-luna"

        outcome, _ = _run(FakeSession(), failing, budget=budget)
        self.assertEqual((outcome.summary, calls), ("Parcial.", ["browser-step", "browser-final"]))

    def test_the_finishing_share_covers_a_final_answer_and_its_check_at_every_bound(self):
        from inference import config

        price_in, price_out = max(
            config.PRICES[provider][model]
            for provider, model in (("openai", "gpt-6-luna"), ("anthropic", "claude-haiku-5-5"))
        )
        output = browser_protocol.MAX_CALL_OUTPUT_TOKENS * price_out
        final = browser_protocol.MAX_REQUEST_TEXT_BYTES + task.IMAGE_TOKENS + task.PROMPT_TOKENS
        check = 4 * browser_protocol.MAX_OBJECTIVE_CHARS + browser_protocol.MAX_SUMMARY_BYTES + task.PROMPT_TOKENS
        needed = sum((bound * price_in + output) * browser_protocol.MAX_CALL_ATTEMPTS for bound in (final, check))
        self.assertGreaterEqual(task.MAX_SPEND * task.FINISH_SHARE // 100, needed)

    def test_screening_a_malformed_answer_stays_linear(self):
        started = time.monotonic()
        screen.urls("](https://a" * (browser_protocol.MAX_SUMMARY_BYTES // 11) * 8)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_hard_deadline_ends_the_task_and_a_call_with_no_time_left_is_never_sent(self):
        ticks = iter([0.0] + [task.MAX_SECONDS + 1.0] * 50)
        self.assertEqual(_run(FakeSession(), Brain(DONE), clock=lambda: next(ticks))[0], task.Outcome("budget"))
        now = [0.0]

        def clock():
            # Each look at the clock is a later moment: the step's own call finds its time already spent.
            now[0] += task.MAX_SECONDS / 4
            return now[0]

        sent = []
        _run(FakeSession(), lambda *args: sent.append(args), clock=clock)
        self.assertEqual(sent, [])

    def test_a_final_answer_ends_with_what_stopped_it(self):
        final = {"summary": "Parcial."}
        outcome, _ = _run(FakeSession(fail_observe=True), Brain(final=final), budget=task.Budget(steps=0))
        self.assertEqual(outcome.reason, "browser-lost")
        stopped = []

        def finishing(operation, request, left):
            stopped.append(operation)
            return final, _counts(), "gpt-6-luna"

        outcome, _ = _run(
            FakeSession(), finishing, budget=task.Budget(steps=0), cancelled=lambda: "browser-final" in stopped
        )
        self.assertEqual(outcome, task.Outcome("stopped"))

    def test_a_read_stops_before_its_first_target_or_while_its_page_loads_when_the_task_must_stop(self):
        links = (("p1:4", "Vaga A"), ("p1:5", "Vaga B"))
        session = FakeSession(links=links, url="https://example.com/busca")
        observation = read.shown(session.page(False), "", "")
        decision = {"kind": "read", "refs": ["p1:4", "p1:5"]}
        self.assertEqual(read.run(session, decision, observation, lambda: False, [].append, print, print), "refused")
        self.assertEqual(session.calls, [])
        answers = iter([True, True, False])
        kept = []
        outcome = read.run(session, decision, observation, lambda: next(answers, False), [].append, print, kept.append)
        self.assertEqual((outcome, session.calls), ("ok", [("click", "p1:4")]))
        self.assertIn("--- not opened: Vaga A", kept[-1])
        # A click the page refused opened nothing, and the read goes on.
        refusing = FakeSession(links=links, url="https://example.com/busca", refuse={"click"})
        kept.clear()
        read.run(
            refusing, decision, read.shown(refusing.page(False), "", ""), lambda: True, [].append, print, kept.append
        )
        self.assertIn("--- not loaded: Vaga A ---", kept[-1])

    def test_a_bracket_that_opens_no_link_and_an_evicted_url_screen_as_prose(self):
        self.assertEqual(screen.urls("veja ](texto) e https://example.com/a"), ["https://example.com/a"])
        checked = screen.Screen()
        checked.observed("https://example.com/first", "")
        for number in range(screen.MAX_URLS):
            checked.observed(f"https://example.com/{number}", "")
        self.assertEqual(checked.unscreened("https://example.com/first"), ["https://example.com/first"])

    def test_screening_compares_whole_url_tokens_exactly(self):
        checked = screen.Screen()
        checked.observed("https://example.com/job", "veja https://example.com/item(2).")
        self.assertEqual(checked.unscreened("https://example.com/job e (https://example.com/item(2))"), [])
        self.assertEqual(checked.unscreened("https://example.com/job/"), ["https://example.com/job/"])
        self.assertEqual(checked.unscreened("https://example.com/item"), ["https://example.com/item"])
        # A Markdown link's destination is taken as written.
        self.assertEqual(checked.unscreened("[vaga](https://example.com/job!)"), ["https://example.com/job!"])
        self.assertEqual(checked.unscreened("[vaga](https://example.com/job)."), [])
        self.assertEqual(screen.urls("[v](https://example.com/job(1).html)"), ["https://example.com/job(1).html"])

    def test_no_step_is_taken_past_the_soft_cutoff_and_stop_during_finishing_wins(self):
        now = [0.0]

        def answering(operation, request, left):
            if operation == "browser-step":
                now[0] = task.MAX_SECONDS - 30.0
                return {"kind": "navigate", "url": "https://example.com/x"}, _counts(), "gpt-6-luna"
            return {"summary": "Parcial."}, _counts(), "gpt-6-luna"

        session = FakeSession()
        outcome, _ = _run(session, answering, clock=lambda: now[0])
        self.assertEqual((outcome.summary, session.calls), ("Parcial.", []))
        stopped = []

        def stopping(operation, request, left):
            if operation == "browser-check":
                stopped.append(True)
                return COMPLETE, _counts(), "gpt-6-luna"
            return DONE, _counts(), "gpt-6-luna"

        self.assertEqual(_run(FakeSession(), stopping, cancelled=lambda: bool(stopped))[0].status, "stopped")

    def test_a_thread_past_its_bound_folds_its_oldest_steps_and_keeps_the_rewritten_memory(self):
        class Long(FakeSession):
            def page(self, screenshot):
                observation = super().page(screenshot)
                text = "x" * browser_protocol.MAX_OBSERVATION_BYTES
                return cua.Observation(observation.url, "Long", text, {}, observation.screenshot)

        memories = iter(["V101: R$ 9.500", *["V101: R$ 9.500\nV102: R$ 7.000"] * 10])
        brain = Brain(*([{"kind": "wait"}] * 12), DONE)
        original = brain.__call__

        def answering(operation, request, left):
            if operation == "browser-fold":
                brain.calls.append((operation, request))
                return {"memory": next(memories)}, _counts(), "gpt-6-luna"
            return original(operation, request, left)

        outcome, _ = _run(Long(), answering)
        self.assertEqual(outcome.status, "done")
        folds = brain.requests("browser-fold")
        self.assertGreaterEqual(len(folds), 2)
        self.assertEqual(folds[1]["thread"]["memory"], "V101: R$ 9.500")
        thread = brain.requests()[-1]["thread"]
        self.assertEqual(thread["memory"], "V101: R$ 9.500\nV102: R$ 7.000")
        self.assertEqual(browser_protocol.canonical_thread(thread), thread)
        for request in brain.requests():
            self.assertLessEqual(browser_protocol.history_bytes(request["thread"]), browser_protocol.MAX_HISTORY_BYTES)
        # A fold whose memory could not be written keeps the memory it had and counts the steps that left unnoted.
        brain = Brain(*([{"kind": "wait"}] * 12), DONE, memory=task.StepFailedError("down"))
        _run(Long(), brain)
        folded = sum(fold["last"] - fold["first"] + 1 for fold in brain.requests("browser-fold"))
        last = brain.requests()[-1]["thread"]
        self.assertEqual((last["memory"], last["unnoted"]), ("", folded))

    def test_an_answer_citing_an_unshown_url_goes_back_twice_then_says_so(self):
        cited = "Prazo: 30/11 (https://example.com/edital)."
        brain = Brain({**DONE, "summary": cited}, {**DONE, "summary": cited}, {**DONE, "summary": cited})
        outcome, _ = _run(FakeSession(), brain)
        self.assertEqual(outcome, _done(cited, 3, unscreened=("https://example.com/edital",)))
        returned = brain.requests()[-1]["thread"]["steps"]
        self.assertEqual([entry["outcome"] for entry in returned], ["returned", "returned"])
        self.assertTrue(returned[0]["step"]["note"].startswith(task.UNSCREENED_NOTE))
        # A cited page the task observed is screened.
        brain = Brain({**DONE, "summary": "Fonte: https://example.com."})
        self.assertEqual(_run(FakeSession(), brain)[0].unscreened, ())

    def test_an_incomplete_answer_goes_back_four_times_then_travels_with_what_is_missing(self):
        missing = {"complete": False, "missing": "Faltam 3 vagas."}
        brain = Brain(*([DONE] * 5), verdicts=[missing] * 5)
        outcome, _ = _run(FakeSession(), brain)
        expected = task.Outcome(
            "done", summary=DONE["summary"], steps=5, completeness="incomplete", missing="Faltam 3 vagas."
        )
        self.assertEqual(outcome, expected)
        notes = [entry["step"]["note"] for entry in brain.requests()[-1]["thread"]["steps"]]
        self.assertEqual(len(notes), 4)
        self.assertTrue(all(note.startswith("Not finished.") for note in notes))
        # A check that failed or answered outside its form is reported as not checked, never as complete.
        for verdict in (task.StepFailedError("down"), {"complete": "yes"}, {"complete": None, "missing": None}):
            with self.subTest(verdict=verdict):
                outcome, _ = _run(FakeSession(), Brain(DONE, verdicts=[verdict]))
                self.assertEqual(outcome.completeness, "not-checked")

    def test_past_the_soft_cutoff_the_brain_answers_from_the_thread_and_never_takes_another_step(self):
        final = {"summary": "Encontrei 7 de 10 vagas."}
        brain = Brain({"kind": "wait"}, final=final)
        ticks = iter([0.0] + [task.MAX_SECONDS - 30.0] * 50)
        outcome, _ = _run(FakeSession(), brain, clock=lambda: next(ticks))
        self.assertEqual(outcome, task.Outcome("done", summary=final["summary"], steps=0, completeness="not-checked"))
        self.assertEqual([name for name, _ in brain.calls], ["browser-final"])
        # The steps run out the same way, and a final answer that could not be written ends with the budget.
        outcome, _ = _run(FakeSession(), Brain({"kind": "wait"}, final=final), budget=task.Budget(steps=1))
        self.assertEqual(outcome.summary, final["summary"])
        outcome, _ = _run(FakeSession(), Brain({"kind": "wait"}, final=None), budget=task.Budget(steps=1))
        self.assertEqual(outcome, task.Outcome("budget", steps=1))

    def test_spend_is_a_ceiling_of_worst_case_reservations_settled_by_reported_usage(self):
        reported = _counts(input_tokens=10_000, output_tokens=1_000)
        brain = Brain({"kind": "wait"}, DONE, counts=reported)
        budget = task.Budget()
        _run(FakeSession(), brain, budget=budget)
        # Three calls (two steps and the check), each settled at the model's price.
        self.assertEqual(budget.spent, 3 * (10_000 * 10 + 1_000 * 50))
        # Attempts whose usage the Brain did not report keep their worst-case reservation.
        unreported = _counts(provider_requests=3, input_tokens=10_000, output_tokens=1_000)
        budget = task.Budget()
        _run(FakeSession(), Brain(DONE, counts=unreported), budget=budget)
        self.assertGreater(
            budget.spent, 2 * (10_000 * 10 + 1_000 * 50) + 2 * browser_protocol.MAX_CALL_OUTPUT_TOKENS * 50
        )
        # A step the normal share cannot reserve finishes the task instead of taking it.
        outcome, _ = _run(FakeSession(), Brain(DONE, final=None), budget=task.Budget(spend=1_000))
        self.assertEqual(outcome, task.Outcome("budget"))
        # A failed call keeps its whole reservation.
        budget = task.Budget()
        _run(FakeSession(), Brain(task.StepFailedError("down"), task.StepFailedError("down")), budget=budget)
        self.assertGreater(budget.spent, 0)

    def test_stop_budgets_and_failures_end_the_task_with_their_closed_reason(self):
        self.assertEqual(_run(FakeSession(), Brain(DONE), cancelled=lambda: True)[0], task.Outcome("stopped"))
        self.assertEqual(
            _run(FakeSession(fail_observe=True), Brain(DONE))[0],
            task.Outcome("failed", reason="browser-lost", cause="observe/tool-failed"),
        )
        down = task.StepFailedError("down")
        self.assertEqual(_run(FakeSession(), Brain(down, down))[0], task.Outcome("failed", reason="model-unavailable"))
        # One failure is asked again: a model answers outside its form now and then.
        self.assertEqual(_run(FakeSession(), Brain(down, DONE))[0], _done("Prazo: 30/11.", 1))
        self.assertEqual(
            _run(FakeSession(), Brain({"kind": "run_script"}))[0],
            task.Outcome("failed", reason="step-invalid", steps=1),
        )
        self.assertEqual(
            _run(FakeSession(fatal={"click"}), Brain({"kind": "click", "ref": "p1:1"}))[0],
            task.Outcome("failed", reason="browser-lost", steps=1, cause="act/tool-failed"),
        )

    def test_every_call_and_action_waits_for_proof_that_the_task_may_continue(self):
        navigate = {"kind": "navigate", "url": "https://example.com/a"}
        # Stop arrives while the Brain answers: its step never reaches the page.
        session = FakeSession()
        stopped = []

        def answering(operation, request, left):
            stopped.append(True)
            return navigate, _counts(), "gpt-6-luna"

        outcome, _ = _run(session, answering, cancelled=lambda: bool(stopped))
        self.assertEqual((outcome, session.calls), (task.Outcome("stopped", steps=1), []))

        # Stop aborts the Brain request in flight: the task was stopped, not failed.
        def aborted(operation, request, left):
            stopped.append(True)
            raise task.StepFailedError("aborted")

        stopped.clear()
        outcome, _ = _run(FakeSession(), aborted, cancelled=lambda: bool(stopped))
        self.assertEqual(outcome, task.Outcome("stopped"))
        # The Browser left the egress process it started on, before or while the Brain answered.
        outcome, _ = _run(FakeSession(), Brain(navigate), intact=lambda: False)
        self.assertEqual(outcome, task.Outcome("failed", reason="browser-lost", cause="runtime/not-intact"))
        lost = []

        def losing(operation, request, left):
            lost.append(True)
            return navigate, _counts(), "gpt-6-luna"

        session = FakeSession()
        outcome, _ = _run(session, losing, intact=lambda: not lost)
        self.assertEqual(
            (outcome, session.calls),
            (task.Outcome("failed", reason="browser-lost", steps=1, cause="runtime/not-intact"), []),
        )

    def test_a_failed_driver_session_is_replaced_and_the_task_continues_where_it_was(self):
        failed = FakeSession(fatal={"click"})
        fresh = FakeSession()
        reopened = []

        def reopen(session, deadline, relaunch):
            reopened.append((session, deadline, relaunch))
            return fresh

        brain = Brain({"kind": "click", "ref": "p1:1"}, {"kind": "scroll", "direction": "down"}, DONE)
        outcome, _ = _run(failed, brain, reopen=reopen)
        self.assertEqual(outcome, _done("Prazo: 30/11.", 3, recovered=1))
        self.assertEqual(reopened, [(failed, task.MAX_SECONDS, False)])
        # The failed session is never used again, and the click whose effect is unknown is in the thread as such.
        self.assertEqual((failed.calls, fresh.calls), ([("click", "p1:1")], [("scroll", "down")]))
        outcomes = [entry["outcome"] for entry in brain.requests()[-1]["thread"]["steps"]]
        self.assertEqual(outcomes, ["interrupted", "ok"])

    def test_an_observation_that_failed_is_looked_at_again_on_a_fresh_session(self):
        fresh = FakeSession()
        brain = Brain(DONE)
        outcome, _ = _run(FakeSession(fail_observe=True), brain, reopen=lambda _session, _deadline, _relaunch: fresh)
        self.assertEqual(outcome, _done("Prazo: 30/11.", 2, recovered=1))
        self.assertEqual((brain.requests()[0]["thread"]["steps"], len(fresh.screenshots)), ([], 1))

    def test_a_page_chromium_no_longer_reads_starts_chromium_again_and_the_agent_is_told(self):
        fresh = FakeSession()
        reopened = []

        def reopen(_session, _deadline, relaunch):
            reopened.append(relaunch)
            return fresh

        brain = Brain(DONE)
        outcome, _ = _run(FakeSession(fail_observe="browser-read-refused"), brain, reopen=reopen)
        self.assertEqual(outcome, replace(_done("Prazo: 30/11.", 2, recovered=1), relaunched=1))
        self.assertEqual(reopened, [True])
        # Only the next observation says so; the one after it does not.
        self.assertTrue(brain.requests()[0]["observation"].startswith(task.RESTARTED_NOTICE))
        brain = Brain({"kind": "scroll", "direction": "down"}, DONE)
        _run(FakeSession(fail_observe="browser-read-refused"), brain, reopen=reopen)
        self.assertFalse(brain.requests()[1]["observation"].startswith(task.RESTARTED_NOTICE))

    def test_a_real_session_whose_page_chromium_no_longer_reads_asks_for_chromium_started_again(self):
        # The Driver's own session over a channel whose every read of the page is refused after the Driver's 20 s.
        refused = test_browser_cua._tool(
            {"status": "refused", "refusal": {"code": "authorization_host_failed", "message": "timed out"}},
            isError=True,
        )
        now = [0.0]
        channel = test_browser_cua.FakeChannel(*test_browser_cua.OPENING, *[refused] * 4)
        sent = channel.send

        def slow(data, deadline):
            if b"get_browser_state" in data and len(channel.sent) > 4:
                now[0] += 20.0
            sent(data, deadline)

        channel.send = slow
        session = cua.CuaSession(channel, clock=lambda: now[0], sleep=lambda seconds: None)
        session.open()
        reopened = []

        def reopen(_session, _deadline, relaunch):
            reopened.append(relaunch)
            return FakeSession()

        outcome, _ = _run(session, Brain(DONE), reopen=reopen, clock=lambda: now[0])
        self.assertEqual((outcome.status, outcome.relaunched, reopened), ("done", 1, [True]))

    def test_recoveries_are_bounded_and_a_failed_one_ends_the_task_with_its_cause(self):
        outcome, _ = _run(
            FakeSession(fail_observe=True),
            Brain(DONE),
            reopen=lambda _session, _deadline, _relaunch: FakeSession(fail_observe=True),
        )
        self.assertEqual(
            outcome,
            task.Outcome(
                "failed", reason="browser-lost", steps=task.MAX_RECOVERIES, cause="observe/tool-failed", recovered=3
            ),
        )

        def refused(_session, _deadline, _relaunch):
            raise cua.CuaError("driver-not-ended")

        outcome, _ = _run(FakeSession(fail_observe=True), Brain(), reopen=refused)
        self.assertEqual(
            outcome,
            task.Outcome("failed", reason="browser-lost", cause="observe/tool-failed/reopen/driver-not-ended"),
        )

    def test_stop_and_a_lost_egress_win_over_a_recovery(self):
        reopened = []

        def reopen(_session, _deadline, _relaunch):
            reopened.append(True)
            raise cua.CuaError("driver-unavailable")

        # Stop came while the session was being replaced.
        outcome, _ = _run(FakeSession(fail_observe=True), Brain(), reopen=reopen, cancelled=lambda: bool(reopened))
        self.assertEqual((outcome, reopened), (task.Outcome("stopped"), [True]))
        # The egress was lost before the session failed: no session is replaced.
        reopened.clear()
        lost = iter([True, False])
        outcome, _ = _run(FakeSession(fail_observe=True), Brain(), reopen=reopen, intact=lambda: next(lost))
        self.assertEqual(outcome, task.Outcome("failed", reason="browser-lost", cause="runtime/not-intact"))
        self.assertEqual(reopened, [])

    def test_no_call_outlives_the_deadline_the_task_was_given(self):
        seconds = []
        now = [10.0]

        def clock():
            now[0] += 1.0
            return now[0]

        session = FakeSession()
        _run(session, Brain({"kind": "wait"}, DONE, seconds=seconds), clock=clock, deadline=500.0)
        self.assertEqual(session.deadline, 500.0)
        self.assertTrue(seconds and all(0 < left < 500.0 - 10.0 for left in seconds))


if __name__ == "__main__":
    unittest.main()
