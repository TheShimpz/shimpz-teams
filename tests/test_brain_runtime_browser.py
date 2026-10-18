"""The Browser's two Brain exchanges: a `browser-required` turn, and one stateless `browser-step`."""

import dataclasses
import json
import unittest
from unittest import mock

from inference import client as brain_runtime_client
from inference import usage as brain_usage
from tests.test_brain_runtime_client import NO_USAGE, RuntimeClientCase, _Response, context

USAGE = {**NO_USAGE, "model_calls": 1, "input_tokens": 1200, "output_tokens": 40}
BROWSER = {"interrupt_id": "call-b", "objective": "Veja o prazo do edital."}


def _turn(**fields):
    return {
        "status": "browser-required",
        "reply": "",
        "clarification": None,
        "actions": [],
        "browser": BROWSER,
        **fields,
    }


class BrowserTurnTests(RuntimeClientCase):
    def test_a_browser_turn_names_exactly_its_one_call_and_the_context_offers_it(self):
        client, connection = self.client(_Response(_turn()))
        offered = dataclasses.replace(context(self.secret), browser=True)
        turn = client.start(offered, "prazo?", conversation=())
        self.assertEqual(turn.status, "browser-required")
        self.assertEqual(turn.browser, brain_runtime_client.BrowserRequest("call-b", BROWSER["objective"]))
        self.assertIs(json.loads(connection.requests[0][2])["browser_available"], True)

    def test_a_browser_call_beside_anything_else_or_out_of_place_is_refused(self):
        action = {"interrupt_id": "call-1", "assistant_id": "hello-pulse", "action": "hello", "input": {}}
        for payload in (
            _turn(reply="text"),
            _turn(actions=[action]),
            _turn(memory=[{"op": "add", "text": "x"}]),
            _turn(routine={"name": "x"}),
            _turn(browser=None),
            _turn(browser={"interrupt_id": "call-b"}),
            _turn(browser={"interrupt_id": "bad id!", "objective": "x"}),
            _turn(browser={"interrupt_id": 7, "objective": "x"}),
            _turn(browser={"interrupt_id": "call-b", "objective": "  padded "}),
            _turn(browser="call"),
            {"status": "completed", "reply": "Hi.", "clarification": None, "actions": [], "browser": BROWSER},
        ):
            with self.subTest(payload=payload), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client, _ = self.client(_Response(payload))
                client.start(context(self.secret), "x", conversation=())


class BrowserStepTests(RuntimeClientCase):
    def test_a_step_returns_the_raw_step_and_its_input_tokens_and_records_the_browser_models_usage(self):
        step = {"kind": "done", "summary": "ok"}
        client, connection = self.client(_Response({"step": step, "model": "gpt-6-luna"}, usage=USAGE))
        request = {"objective": "x", "language": "en", "history": [], "observation": "url: x", "screenshot": None}
        with mock.patch.object(brain_usage, "record") as record:
            self.assertEqual(client.browser_step(context(self.secret), request), (step, 1200))
        record.assert_called_once_with("browser-step", "openai", "gpt-6-luna", USAGE)
        method, path, raw_body, _headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/browser-step"))
        body = json.loads(raw_body)
        self.assertEqual(body["provider"]["api_key"], self.secret)
        self.assertEqual((body["objective"], body["observation"]), ("x", "url: x"))

    def test_a_malformed_step_answer_is_refused(self):
        for payload, usage in (
            ({"step": {}, "model": "gpt-6-luna", "extra": 1}, USAGE),
            ({"step": {}, "model": "not a model!"}, USAGE),
            ({"step": {}, "model": 7}, USAGE),
            ({"step": {}, "model": "gpt-6-luna"}, {"model_calls": "one"}),
            (["step"], None),
        ):
            with self.subTest(payload=payload), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client, _ = self.client(_Response(payload, usage=usage))
                client.browser_step(context(self.secret), {})


if __name__ == "__main__":
    unittest.main()
