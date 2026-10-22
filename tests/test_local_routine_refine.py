"""A recorded Routine is refined from its chat by explicit edits and changes only through its card (ADR-0122)."""

import contextlib
import dataclasses
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import routine_fixture
from test_local_routine_proposal import ASSISTANT, MESSAGE, SHIMPZ, ZONES, RecordingCase, _body, _record

from inference import client as brain_runtime_client
from local import app as local_app
from local.chat import api as local_chat_api
from local.chat import segment as local_segment
from local.routine import refine as routine_refine
from protocol.http.v1 import routine_context as http_routine_context
from protocol.http.v1 import routine_proposal as http_routine_proposal
from routine import record

ZONES_CALL = ("list-zones", {"page": 1, "per_page": 25})
RECORDS_CALL = ("list-dns-records", {"zone_id": SHIMPZ, "page": 1, "per_page": 25})
NEXT_PAGE = ("list-zones", {"page": 2, "per_page": 25})
NAMED = "Na rotina DNS de shimpz.com, liste também a página 2 das zonas"
DESCRIPTION = "Lista as zonas da Cloudflare, lê os registros DNS de shimpz.com e lista a página 2 das zonas."


def _refine(*edits: dict[str, object], description: str = DESCRIPTION, revision: int | None = None):
    """The agent's refine of the Routine its turn was shown, at the revision it was shown unless one is given."""

    def outcome(context) -> dict[str, object]:
        shown = context.routine_selected
        return {
            "routine_id": shown["routine_id"],
            "base_revision": shown["revision"] if revision is None else revision,
            "description": description,
            "edits": list(edits),
        }

    return outcome


class Script:
    """A scripted chat agent: each turn runs its calls in order, then completes with its outcome, if any."""

    purpose = staticmethod(lambda *_args: None)

    def __init__(self, *turns: tuple[tuple[tuple[str, dict[str, object]], ...], object]) -> None:
        self.turns = list(turns)
        self.contexts: list[brain_runtime_client.RuntimeContext] = []
        self.results: list[dict[str, object]] = []
        self.between = lambda: None

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        self.calls, self.outcome = self.turns.pop(0)
        self.done = 0
        return self._next()

    def resume(self, _context, results):
        self.results.append(results)
        return self._next()

    def _next(self):
        if self.done < len(self.calls):
            action, given = self.calls[self.done]
            self.done += 1
            request = brain_runtime_client.ActionRequest(
                f"i-{len(self.contexts)}-{self.done}", ASSISTANT, action, given
            )
            return brain_runtime_client.RuntimeTurn("action-required", "", (request,))
        self.between()
        outcome = self.outcome(self.contexts[-1]) if callable(self.outcome) else self.outcome
        return brain_runtime_client.RuntimeTurn("completed", "Pronto.", (), routine=outcome)


class RefineCase(RecordingCase):
    def created(self, directory: str, *turns) -> tuple[object, Script, str]:
        """A Team holding the owner's confirmed DNS Routine, whose agent then runs ``turns``."""
        runtime = Script(((ZONES_CALL, RECORDS_CALL), _record()), *turns)
        service = self.controller(directory, runtime)
        card = self.chat(service, _body(MESSAGE))["routine_proposal"]
        return service, runtime, self.confirm(service, card["proposal_id"])["routine_id"]

    def refined(self, service, message: str = NAMED, refine: str | None = None) -> dict[str, object]:
        return self.chat(service, _body(message, refine=refine))


class RefinementTests(RefineCase):
    def test_a_named_routine_is_selected_and_an_insertion_confirms_as_its_next_revision(self) -> None:
        insert = {"kind": "insert", "step": "s2", "calls": ["c1"]}
        with tempfile.TemporaryDirectory() as directory:
            service, runtime, routine_id = self.created(directory, ((NEXT_PAGE,), _refine(insert)))
            before = record.routine(service.routine_store.load("team_1"), routine_id)
            response = self.refined(service)
            card = response["routine_refinement"]
            answer = self.confirm(service, card["proposal_id"])
            again = self.confirm(service, card["proposal_id"])
            state = service.routine_store.load("team_1")
        recording, refining = runtime.contexts
        self.assertIsNone(recording.routine_selected)
        self.assertEqual(runtime.results[0], {"i-1-1": ZONES})
        selected = refining.routine_selected
        self.assertEqual(http_routine_context.canonical_selected_routine(selected), selected)
        self.assertEqual((selected["routine_id"], selected["revision"]), (routine_id, 1))
        self.assertEqual(selected["description"], _record()["description"])
        self.assertEqual([step["id"] for step in selected["steps"]], ["s1", "s2"])
        self.assertIsNone(selected["last_run"])
        self.assertTrue(refining.routine_mode)
        self.assertEqual(refining.model, local_segment.ROUTINE_MODELS["openai"])
        self.assertEqual(refining.turn_clock[1], before.timezone)
        self.assertEqual(runtime.results[-1], {"i-2-1": {"call": "c1", "result": ZONES}})
        self.assertEqual(http_routine_proposal.canonical_refinement(card), card)
        self.assertEqual((card["routine_id"], card["base_revision"], card["last_changed"]), (routine_id, 1, True))
        self.assertEqual(
            [(step["change"], step["base"]) for step in card["steps"]], [("kept", 1), ("kept", 2), ("added", None)]
        )
        self.assertEqual((card["removed"], card["description"], card["schedule"]), ([], DESCRIPTION, before.schedule))
        self.assertEqual(answer, {**again, "status": "changed", "routine_id": routine_id})
        (routine,) = state.routines
        self.assertEqual((routine.revision, routine.description, routine.name), (2, DESCRIPTION, before.name))
        self.assertEqual(
            [step["action"] for step in routine.plan["steps"]], ["list-zones", "list-dns-records", "list-zones"]
        )
        self.assertEqual(
            routine.plan["steps"][2]["input"],
            {"page": {"kind": "literal", "value": 2}, "per_page": {"kind": "literal", "value": 25}},
        )
        self.assertEqual(
            (routine.anchor, routine.next_run_at, routine.schedule),
            (before.anchor, before.next_run_at, before.schedule),
        )
        self.assertEqual(state.notices[-1].outcome, "changed")

    def test_the_refine_entry_selects_its_routine_and_a_removal_keeps_the_rest(self) -> None:
        remove = {"kind": "remove", "step": "s2", "calls": []}
        with tempfile.TemporaryDirectory() as directory:
            service, runtime, routine_id = self.created(directory, ((), _refine(remove)))
            card = self.refined(service, "Tire a leitura dos registros", refine=routine_id)["routine_refinement"]
        self.assertEqual(runtime.contexts[-1].routine_selected["routine_id"], routine_id)
        self.assertEqual([step["change"] for step in card["steps"]], ["kept"])
        self.assertEqual(
            card["removed"],
            [
                {
                    "position": 2,
                    "assistant": ASSISTANT,
                    "action": "list-dns-records",
                    "read_only": True,
                    "change": "removed",
                }
            ],
        )
        self.assertTrue(card["last_changed"])

    def test_an_unresolved_or_ambiguous_reference_selects_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, runtime, routine_id = self.created(directory, ((), None), ((), None), ((), None))
            other = dataclasses.replace(
                record.routine(service.routine_store.load("team_1"), routine_id), routine_id="f" * 32, name="Zonas"
            )
            service.routine_store.update("team_1", lambda state: (record.add_routine(state, other), None))
            self.refined(service, "Ajuste a rotina", refine="e" * 32)
            self.refined(service, "Ajuste DNS de shimpz.com e Zonas")
            self.refined(service, "Bom dia")
        self.assertEqual([context.routine_selected for context in runtime.contexts[1:]], [None, None, None])

    def test_a_dependency_an_unknown_call_or_a_sourced_input_refuses_the_card(self) -> None:
        cases = {
            "routine-refine-dependency": ((), _refine({"kind": "remove", "step": "s1", "calls": []})),
            "routine-refine-invalid": ((), _refine({"kind": "insert", "step": "s2", "calls": ["c1"]})),
            "routine-refine-input": (
                (ZONES_CALL, RECORDS_CALL),
                _refine({"kind": "insert", "step": "s2", "calls": ["c2"]}),
            ),
        }
        for code, turn in cases.items():
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                service, _runtime, _routine_id = self.created(directory, turn)
                response = self.refined(service)
                self.assertNotIn("routine_refinement", response)
                self.assertEqual(response["routine_refusal"], {"code": code})
                self.assertEqual(service.routine_store.load("team_1").routines[0].revision, 1)

    def test_a_description_that_states_another_schedule_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _runtime, _routine_id = self.created(
                directory, ((), _refine(description="Lista o DNS todo dia às 9h."))
            )
            self.assertEqual(self.refined(service)["routine_refusal"], {"code": "routine-description-conflict"})

    def test_an_edit_of_a_routine_or_revision_the_turn_was_not_shown_is_a_brain_contract_failure(self) -> None:
        def stranger(context) -> dict[str, object]:
            return {**_refine()(context), "routine_id": "f" * 32}

        for outcome in (_refine(revision=2), stranger, {"routine_id": "f" * 32, "edits": []}):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                service, _runtime, _routine_id = self.created(directory, ((), outcome))
                with self.assertRaises(local_app.ApiProblem) as caught:
                    self.refined(service)
                self.assertEqual(caught.exception.code, "brain-runtime-failed")


class AuthorityTests(RefineCase):
    def test_a_stale_revision_or_a_live_run_refuses_the_card_and_its_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, runtime, routine_id = self.created(
                directory, ((), _refine()), ((), _refine()), ((), _refine()), ((), _refine())
            )
            runtime.between = lambda: routine_fixture.update_routine(service, routine_id, revision=2)
            stale = self.refined(service)
            runtime.between = lambda: None
            card = self.refined(service)["routine_refinement"]
            routine_fixture.update_routine(service, routine_id, revision=3)
            with self.assertRaises(local_app.ApiProblem) as moved:
                self.confirm(service, card["proposal_id"])
            frozen = routine_fixture.put_frozen_run(service, routine_id, ASSISTANT)
            busy = self.refined(service)
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=()), None))
            card = self.refined(service)["routine_refinement"]
            routine_fixture.put_frozen_run(service, routine_id, ASSISTANT, run_id=frozen.run_id)
            with self.assertRaises(local_app.ApiProblem) as live:
                self.confirm(service, card["proposal_id"])
        self.assertEqual(stale["routine_refusal"], {"code": "routine-revision-changed"})
        self.assertEqual(moved.exception.code, "routine-revision-changed")
        self.assertEqual(busy["routine_refusal"], {"code": "routine-busy"})
        self.assertEqual(live.exception.code, "routine-busy")

    def test_a_run_that_ended_while_the_card_was_open_is_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _runtime, routine_id = self.created(
                directory, ((NEXT_PAGE,), _refine({"kind": "insert", "step": "s2", "calls": ["c1"]}))
            )
            card = self.refined(service)["routine_refinement"]
            current = record.routine(service.routine_store.load("team_1"), routine_id)
            # A run started and ended meanwhile: the next firing moved one interval on, and the person paused it.
            later = current.next_run_at + current.schedule["gap"]
            routine_fixture.update_routine(service, routine_id, next_run_at=later, paused=True)
            self.confirm(service, card["proposal_id"])
            routine = record.routine(service.routine_store.load("team_1"), routine_id)
        self.assertEqual((routine.revision, routine.next_run_at, routine.paused), (2, later, True))

    def test_a_cancelled_card_changes_nothing_and_its_demonstration_stays_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, runtime, routine_id = self.created(
                directory, ((NEXT_PAGE,), _refine({"kind": "insert", "step": "s2", "calls": ["c1"]}))
            )
            response = self.refined(service)
            with self.as_person():
                answer = service.revoke_routine_proposal("team_1", response["routine_refinement"]["proposal_id"])
            routine = record.routine(service.routine_store.load("team_1"), routine_id)
        self.assertEqual(answer["status"], "revoked")
        self.assertEqual((routine.revision, len(routine.plan["steps"])), (1, 2))
        self.assertEqual((response["reply"], len(runtime.results)), ("Pronto.", 3))

    def test_another_teams_routine_is_never_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, runtime, _routine_id = self.created(directory, ((), None))
            self.refined(service, "Ajuste a rotina", refine="0" * 32)
        self.assertIsNone(runtime.contexts[-1].routine_selected)


INSERT = {"kind": "insert", "step": "s2", "calls": ["c1"]}


class RefusalTests(RefineCase):
    def refusal(self, prepare) -> dict[str, object]:
        """The terminal of one refinement turn whose card ``prepare`` sets up to fail."""
        with tempfile.TemporaryDirectory() as directory:
            service, runtime, routine_id = self.created(directory, ((NEXT_PAGE,), _refine(INSERT)))
            with prepare(service, runtime, routine_id):
                response = self.refined(service)
            self.assertEqual(record.routine(service.routine_store.load("team_1"), routine_id).revision, 1)
        self.assertNotIn("routine_refinement", response)
        return response["routine_refusal"]["code"]

    def test_a_card_that_cannot_be_kept_or_shown_is_refused_whole(self) -> None:
        def deleted(service, runtime, routine_id):
            runtime.between = lambda: routine_fixture.update_routine(service, routine_id, deleting=True)
            return contextlib.nullcontext()

        def protected(service, runtime, _routine_id):
            # The span came to protect a value the kept steps show, such as the zone a selector names.
            def protect() -> None:
                span = service.routine_recordings._spans["team_1"]
                service.routine_recordings.protect("team_1", span.recording_id, ["shimpz.com"])

            runtime.between = protect
            return contextlib.nullcontext()

        cases = {
            "routine-not-found": deleted,
            "routine-step-budget": lambda *_args: mock.patch.object(
                routine_refine.routine_definition, "over_budget", return_value="routine-step-budget"
            ),
            "routine-proposal-too-large": lambda *_args: mock.patch.object(
                http_routine_proposal, "MAX_PROPOSAL_BYTES", 64
            ),
            "routine-secret-literal": protected,
        }
        for code, prepare in cases.items():
            with self.subTest(code=code):
                self.assertEqual(self.refusal(prepare), code)
        line = mock.patch.object(local_chat_api.http_progress, "MAX_LINE_BYTES", 1024)
        self.assertEqual(self.refusal(lambda *_args: line), "routine-proposal-too-large")

    def test_an_invalid_card_or_refine_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _runtime, _routine_id = self.created(directory, ((NEXT_PAGE,), _refine(INSERT)))
            with self.assertRaises(local_app.ApiProblem) as malformed:
                self.refined(service, refine="NOT-AN-ID")
            invalid = mock.patch.object(http_routine_proposal, "canonical_refinement", return_value=None)
            with invalid, self.assertRaises(local_app.ApiProblem) as caught:
                self.refined(service)
        self.assertEqual(malformed.exception.code, "invalid-refine")
        self.assertEqual(caught.exception.code, "internal-error")


class SelectedViewTests(unittest.TestCase):
    def test_a_routine_no_longer_listed_or_too_large_to_show_is_not_sent(self) -> None:
        state = routine_fixture.added(routine_fixture.routine())
        service = SimpleNamespace(routine_store=SimpleNamespace(load=lambda _team: state))
        with mock.patch.object(routine_refine.routine_diagnostics, "last_run", return_value=None):
            self.assertIsNone(routine_refine.chat_selected(service, "team_1", SimpleNamespace(selected="f" * 32)))
            shown = routine_refine.chat_selected(service, "team_1", SimpleNamespace(selected="a" * 32))
            self.assertEqual((shown[0]["routine_id"], shown[1]), ("a" * 32, "UTC"))
            with mock.patch.object(http_routine_context, "canonical_selected_routine", return_value=None):
                self.assertIsNone(routine_refine.chat_selected(service, "team_1", SimpleNamespace(selected="a" * 32)))


if __name__ == "__main__":
    unittest.main()
