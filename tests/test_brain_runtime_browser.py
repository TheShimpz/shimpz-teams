"""The Browser's Brain exchanges: a `browser-required` turn, and a task's stateless Browser operations."""

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


class BrowserCallTests(RuntimeClientCase):
    def test_a_step_returns_the_raw_step_its_usage_and_model_and_records_the_browser_models_usage(self):
        step = {"kind": "done", "summary": "ok"}
        client, connection = self.client(_Response({"step": step, "model": "gpt-6-luna"}, usage=USAGE))
        request = {"objective": "x", "language": "en", "observation": "url: x", "screenshot": None}
        # A chat turn meters every Brain operation it makes, the Browser's steps included.
        with brain_usage.metered(), mock.patch.object(client, "_post", wraps=client._post) as post:
            answer, counts, model = client.browser_call(context(self.secret), "browser-step", request, deadline=60.0)
            [entry] = brain_usage.drain()
        self.assertEqual((answer, counts["input_tokens"], model), (step, 1200, "gpt-6-luna"))
        named = (entry["operation"], entry["provider"], entry["model"])
        self.assertEqual(named, ("browser-step", "openai", "gpt-6-luna"))
        self.assertEqual((entry["count"], entry["input_tokens"]), (1, USAGE["input_tokens"]))
        # The call's request ends with the task's time left.
        self.assertEqual(post.call_args.kwargs, {"deadline": 60.0})
        method, path, raw_body, _headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/browser-step"))
        body = json.loads(raw_body)
        self.assertEqual(body["provider"]["api_key"], self.secret)
        self.assertEqual((body["objective"], body["observation"]), ("x", "url: x"))

    def test_each_finishing_operation_answers_its_own_fields_under_its_own_path_and_meter(self):
        for operation, answer in (
            ("browser-fold", {"memory": "V101: R$ 9.500"}),
            ("browser-check", {"complete": False, "missing": "Faltam vagas."}),
            ("browser-final", {"summary": "Encontrei 7 vagas."}),
        ):
            with self.subTest(operation=operation):
                client, connection = self.client(_Response({**answer, "model": "gpt-6-luna"}, usage=USAGE))
                with brain_usage.metered():
                    got, _counts, _model = client.browser_call(context(self.secret), operation, {}, deadline=9.0)
                    [entry] = brain_usage.drain()
                self.assertEqual((got, entry["operation"]), (answer, operation))
                self.assertEqual(connection.requests[0][1], f"/v1/{operation}")

    def test_a_malformed_answer_is_refused(self):
        for operation, payload, usage in (
            ("browser-step", {"step": {}, "model": "gpt-6-luna", "extra": 1}, USAGE),
            ("browser-step", {"step": {}, "model": "not a model!"}, USAGE),
            ("browser-step", {"step": {}, "model": 7}, USAGE),
            ("browser-step", {"step": {}, "model": "gpt-6-luna"}, {"model_calls": "one"}),
            ("browser-step", ["step"], None),
            ("browser-fold", {"step": {}, "model": "gpt-6-luna"}, USAGE),
            ("browser-check", {"complete": True, "model": "gpt-6-luna"}, USAGE),
        ):
            with self.subTest(payload=payload), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client, _ = self.client(_Response(payload, usage=usage))
                client.browser_call(context(self.secret), operation, {}, deadline=60.0)


if __name__ == "__main__":
    unittest.main()
