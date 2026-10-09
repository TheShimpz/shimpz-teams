"""Long journeys learn whole procedures and keep a Team's skills bounded, fresh, and contract-bound (ADR-0085).

A multi-round, multi-Assistant task paused for a human and resumed after a restart still learns every step.
"""

import dataclasses
import unittest

from test_chat_orchestrator import FakeRuntime, completed, strategy
from test_local_chat_continuations import pending

from action import human as action_human
from chat import knowledge as chat_knowledge
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from local.chat import continuation as local_chat_continuations
from local.chat import continuation_store as local_chat_continuation_store
from protocol.http.v1 import payload as http_payload
from tests import human_request_fixtures

_OBJECT = {"type": "object", "additionalProperties": False}


def _action(action_id: str, *inputs: str) -> brain_runtime_client.RuntimeAction:
    return brain_runtime_client.RuntimeAction(
        action_id, f"Run {action_id}.", {**_OBJECT, "properties": {name: {"type": "string"} for name in inputs}}
    )


DNS = brain_runtime_client.RuntimeAssistant(
    "shimpz-cloudflare",
    "Manage DNS.",
    (
        _action("list-zones"),
        _action("list-dns-records", "zone_id", "name"),
        _action("replace-dns-record", "zone_id", "record_id", "type", "name", "content"),
    ),
)
SEARCH = brain_runtime_client.RuntimeAssistant(
    "shimpz-exa", "Research the web.", (_action("search-web", "query"), _action("read-pages", "urls"))
)
# One long journey: find the zone and record, research twice, read, then change the record behind an approval.
JOURNEY = (
    ("shimpz-cloudflare", "list-zones", {}),
    ("shimpz-cloudflare", "list-dns-records", {"zone_id": "z", "name": "www.example.com"}),
    ("shimpz-exa", "search-web", {"query": "new origin address"}),
    ("shimpz-exa", "search-web", {"query": "origin address confirmation"}),
    ("shimpz-exa", "read-pages", {"urls": "https://example.com/status"}),
    (
        "shimpz-cloudflare",
        "replace-dns-record",
        {"zone_id": "z", "record_id": "r", "type": "A", "name": "www.example.com", "content": "198.51.100.7"},
    ),
)


def _context(*assistants: brain_runtime_client.RuntimeAssistant) -> brain_runtime_client.RuntimeContext:
    return brain_runtime_client.RuntimeContext(
        thread_id="team:journey:thread",
        team_name="Journey Team",
        assistants=assistants or (DNS, SEARCH),
        provider="openai",
        model="gpt-6-luna",
        api_key="sk-journey-0123456789",
        effort="low",
        memories=(),
        skills=(),
    )


def _rounds(journey=JOURNEY) -> list[brain_runtime_client.RuntimeTurn]:
    return [
        brain_runtime_client.RuntimeTurn(
            status="action-required",
            reply="",
            actions=(brain_runtime_client.ActionRequest(f"step-{index}", assistant, action, dict(values)),),
        )
        for index, (assistant, action, values) in enumerate(journey)
    ]


def _approval() -> action_human.AdmittedHumanRequest:
    descriptor = {"kind": "approval", "ordinal": 0, "title": "Change DNS", "description": "Replace the record."}
    return human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("approval",))


class LongJourneyTests(unittest.TestCase):
    def _persist_and_restore(self, suspension: chat_orchestrator.ChatHumanSuspension):
        """Store the paused journey exactly as a Local continuation does and read it back, as after a restart."""
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant", "Demo Assistant", ("publish",), (("cloudflare", "cloudflare", ("dns.read",)),)
            ),
        )
        paused = dataclasses.replace(pending(), continuation=suspension.continuation)
        bindings, payload = local_chat_continuations.encode("integrations", requirements, paused)
        stored = local_chat_continuation_store.StoredContinuation(
            "team_1", "integrations", "c" * 32, 1_300, 1, bindings, payload
        )
        return local_chat_continuations.decode(stored).pending.continuation

    def test_a_paused_and_resumed_journey_learns_every_step_of_its_procedure(self):
        approved = False

        def invoke(request):
            if request.action == "replace-dns-record" and not approved:
                raise action_human.HumanRequestSuspensionError(_approval())
            return {"ok": True}

        runtime = FakeRuntime([*_rounds(), completed("Updated.")])
        paused = chat_orchestrator.run_until_pause(
            runtime, _context(), "Update www to the new origin.", strategy(lambda *_args: _args[2], invoke)
        )
        self.assertIsInstance(paused, chat_orchestrator.ChatHumanSuspension)
        self.assertEqual([action.action for action in paused.continuation.invoked], [step[1] for step in JOURNEY[:5]])

        restored = self._persist_and_restore(paused)
        self.assertEqual(restored.invoked, paused.continuation.invoked)
        approved = True
        outcome = chat_orchestrator.continue_after_pause(
            runtime, _context(), restored, strategy(lambda *_args: _args[2], invoke)
        )
        self.assertIsInstance(outcome, chat_orchestrator.ChatOutcome)

        skill = chat_knowledge.learned_skill(outcome.actions)
        self.assertEqual(
            [(step["assistant_id"], step["action"], step["inputs"]) for step in skill["steps"]],
            [(assistant, action, sorted(values)) for assistant, action, values in JOURNEY],
        )
        self.assertEqual(
            skill["contracts"],
            {assistant.id: brain_runtime_client.contract_digest(assistant) for assistant in (DNS, SEARCH)},
        )
        # Repeated searches stay: they were both part of what worked.
        self.assertEqual([step["action"] for step in skill["steps"]].count("search-web"), 2)
        self.assertNotIn("198.51.100.7", repr(skill))

    def test_many_tasks_keep_the_newest_procedures_and_repeating_one_refreshes_it(self):
        def journey_skill(*actions: str) -> dict[str, object]:
            invoked = tuple(
                chat_orchestrator.InvokedAction("shimpz-exa", action, ("query",), "sha256:" + "e" * 64)
                for action in actions
            )
            return chat_knowledge.learned_skill(invoked)

        first = journey_skill("search-web", "read-pages")
        memory, skills = http_payload.apply_knowledge([], [], [], first)
        for index in range(http_payload.MAX_SKILLS - 1):
            memory, skills = http_payload.apply_knowledge(
                memory, skills, [], journey_skill(*["search-web"] * (index + 2), "read-pages")
            )
        self.assertEqual(len(skills), http_payload.MAX_SKILLS)
        self.assertEqual(skills[0]["key"], first["key"])
        # Doing the first task again makes it the newest, so the next new task evicts the second oldest instead.
        _memory, skills = http_payload.apply_knowledge(memory, skills, [], first)
        second_oldest = skills[0]["key"]
        _memory, skills = http_payload.apply_knowledge(memory, skills, [], journey_skill("read-pages", "read-pages"))
        self.assertIn(first["key"], {skill["key"] for skill in skills})
        self.assertNotIn(second_oldest, {skill["key"] for skill in skills})
        self.assertEqual(len(skills), http_payload.MAX_SKILLS)

    def test_a_journey_too_long_to_learn_and_a_changed_assistant_are_handled_safely(self):
        long_journey = tuple(
            chat_orchestrator.InvokedAction("shimpz-exa", "search-web", ("query",), "sha256:" + "e" * 64)
            for _step in range(http_payload.MAX_SKILL_STEPS + 1)
        )
        self.assertIsNone(chat_knowledge.learned_skill(long_journey))
        learned = chat_knowledge.learned_skill(
            tuple(
                chat_orchestrator.InvokedAction(
                    assistant,
                    action,
                    tuple(sorted(values)),
                    brain_runtime_client.contract_digest(DNS if assistant == DNS.id else SEARCH),
                )
                for assistant, action, values in JOURNEY
            )
        )
        changed = dataclasses.replace(SEARCH, genesis="Research the web, now with a new contract.")
        self.assertEqual([skill["usable"] for skill in chat_knowledge.turn_skills([learned], (DNS, SEARCH))], [True])
        self.assertEqual([skill["usable"] for skill in chat_knowledge.turn_skills([learned], (DNS, changed))], [False])
        self.assertEqual([skill["usable"] for skill in chat_knowledge.turn_skills([learned], (DNS,))], [False])


if __name__ == "__main__":
    unittest.main()
