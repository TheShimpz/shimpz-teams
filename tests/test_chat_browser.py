"""A chat turn's one Browser task: offered only where admitted, run alone, and followed only by the reply."""

import dataclasses
import unittest

from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from chat import turn as chat_turn
from inference import client as brain_runtime_client
from tests.test_chat_orchestrator import accept_input, completed, context

REQUEST = brain_runtime_client.BrowserRequest("interrupt-b", "Veja o prazo do edital.")
RESULT = {"status": "done", "summary": "Prazo: 30/11.", "files": []}
TERMINAL = {"files": [], "lost": [], "skipped": 0}


def browser_turn() -> brain_runtime_client.RuntimeTurn:
    return brain_runtime_client.RuntimeTurn(status="browser-required", reply="", actions=(), browser=REQUEST)


class Runtime:
    def __init__(self, start, *resumes):
        self.starts, self.resumes = [start], list(resumes)
        self.resumed = []

    def start(self, _context, _message, *, conversation):
        return self.starts.pop(0)

    def resume(self, _context, results):
        self.resumed.append(results)
        return self.resumes.pop(0)


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append((event["phase"], event["state"]))


def strategy(run_browser=None, *, cancelled=lambda: False, recorder=None):
    return chat_orchestrator.ChatStrategy(
        validate_action=accept_input,
        invoke_action=lambda _request: {"ok": True},
        cancelled=cancelled,
        progress=chat_progress.Reporter(recorder or (lambda _event: None)),
        run_browser=run_browser,
    )


def offered():
    return dataclasses.replace(context(), browser=True)


class ChatBrowserTests(unittest.TestCase):
    def test_the_task_runs_once_and_the_reply_carries_its_terminal_field(self):
        calls = []
        recorder = Recorder()
        runtime = Runtime(browser_turn(), completed("O prazo é 30/11."))

        def run_browser(turn_context, request):
            calls.append((turn_context.thread_id, request))
            return RESULT, TERMINAL

        outcome = chat_orchestrator.run_until_pause(
            runtime, offered(), "prazo?", strategy(run_browser, recorder=recorder)
        )
        self.assertEqual((outcome.reply, outcome.browser), ("O prazo é 30/11.", TERMINAL))
        self.assertEqual(calls, [("team:assistant:conversation", REQUEST)])
        self.assertEqual(runtime.resumed, [{"interrupt-b": RESULT}])
        self.assertIn(("browser", "started"), recorder.events)
        self.assertIn(("browser", "finished"), recorder.events)
        body = chat_turn.terminal_body("team_1", "Team", outcome, None)
        self.assertEqual(body["browser"], TERMINAL)

    def test_the_browser_is_refused_where_it_was_not_offered(self):
        def run_browser(_context, _request):
            return RESULT, TERMINAL

        cases = [
            (context(), run_browser),
            (dataclasses.replace(offered(), attachments=({"id": "f"},)), run_browser),
            (offered(), None),
        ]
        for turn_context, runner in cases:
            with (
                self.subTest(turn_context=turn_context.browser, runner=runner),
                self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "not offered"),
            ):
                chat_orchestrator.run_until_pause(Runtime(browser_turn()), turn_context, "x", strategy(runner))
        missing = dataclasses.replace(browser_turn(), browser=None)
        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "not offered"):
            chat_orchestrator.run_until_pause(Runtime(missing), offered(), "x", strategy(run_browser))

    def test_after_a_browser_result_the_turn_may_only_reply(self):
        def runner(_context, _request):
            return RESULT, TERMINAL

        for resumed in (
            browser_turn(),
            dataclasses.replace(completed(), memory=({"op": "add", "text": "x"},)),
            dataclasses.replace(completed(), routine={"name": "x"}),
            dataclasses.replace(completed(), clarification={"question": "x"}),
            brain_runtime_client.RuntimeTurn(
                status="action-required",
                reply="",
                actions=(brain_runtime_client.ActionRequest("i", "hello-pulse", "hello", {}),),
            ),
        ):
            with (
                self.subTest(resumed=resumed.status),
                self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "more than reply"),
            ):
                chat_orchestrator.run_until_pause(Runtime(browser_turn(), resumed), offered(), "x", strategy(runner))

    def test_a_browser_call_in_the_last_round_still_ends_with_its_reply(self):
        runtime = Runtime(browser_turn(), completed("O prazo é 30/11."))
        last = dataclasses.replace(strategy(lambda _context, _request: (RESULT, TERMINAL)), max_rounds=0)
        outcome = chat_orchestrator.run_until_pause(runtime, offered(), "prazo?", last)
        self.assertEqual((outcome.reply, outcome.browser), ("O prazo é 30/11.", TERMINAL))

    def test_stop_during_the_task_ends_the_turn_before_the_brain_resumes(self):
        stopped = [False]

        def run_browser(_context, _request):
            stopped[0] = True
            return {"status": "stopped", "files": []}, TERMINAL

        runtime = Runtime(browser_turn(), completed())
        with self.assertRaises(chat_orchestrator.ChatStoppedError):
            chat_orchestrator.run_until_pause(
                runtime, offered(), "x", strategy(run_browser, cancelled=lambda: stopped[0])
            )
        self.assertEqual(runtime.resumed, [])


if __name__ == "__main__":
    unittest.main()
