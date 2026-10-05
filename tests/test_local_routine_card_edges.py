"""A held run's card refuses every stale, drifted, or unmatched Rodar and Recriar and changes nothing (ADR-0092)."""

from __future__ import annotations

import dataclasses
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

from test_local_routine_card import DAILY, MESSAGE, PRINCIPAL, CardCase, RecriarTests, _change, _compiled, _origin
from test_local_routine_recovery import Assistant, failed

from inference import config as inference_config
from local.routine import card as routine_card
from local.routine import manage as routine_manage
from local.routine import recreate as routine_recreate
from local.routine import state as routine_state
from routine import record
from routine.request import Request as RoutineRequest

CLARIFICATION = {
    "question": "When?",
    "options": [{"label": "At 10", "description": ""}, {"label": "At 9", "description": ""}],
    "default_index": None,
}


def _asked(**question: object) -> dict[str, object]:
    asked = _change(schedule=None)
    asked["question"] = {
        "field": {"kind": "schedule"},
        "values": [{"kind": "daily", "time": "10:00"}, DAILY],
        "replies": ["Pronto: 10:00.", "Pronto: 09:00."],
        **question,
    }
    return asked


def _update_routine(service, routine_id: str, **changes: object) -> None:
    service.routine_store.update(
        "team_1",
        lambda state: (
            record._replace_routine(state, dataclasses.replace(record.routine(state, routine_id), **changes)),
            None,
        ),
    )


class CardBindingTests(CardCase):
    def test_a_card_opened_while_its_source_is_unreadable_goes_stale_once_the_source_reads_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            self.seal(service, value)
            with mock.patch.object(routine_card.routine_source, "load", side_effect=routine_state.unavailable()):
                card = self.card(service, run_id)
            with self.assertRaises(routine_card.ApiProblem) as caught:
                self.answer(service, run_id, card, "run")
            state = self.state(service)
        self.assertEqual(caught.exception.code, "routine-card-stale")
        self.assertEqual([item.status for item in state.incidents], ["unresolved"])

    def test_a_diagnostic_recorded_for_another_step_is_shown_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            read = service.routine_diagnostics.read

            def elsewhere(*args):
                # The held operation's own record, but naming another Action than the held step.
                return [
                    SimpleNamespace(
                        operation_id=item.operation_id, view=lambda item=item: {**item.view(), "action": "x"}
                    )
                    for item in read(*args)
                ]

            self.assertEqual(self.card(service, run_id)["evidence"], "recorded")
            with mock.patch.object(service.routine_diagnostics, "read", side_effect=elsewhere):
                card = self.card(service, run_id)
        self.assertEqual((card["evidence"], card["diagnostic"]), ("absent", None))

    def test_a_card_opened_while_the_routine_is_deleting_restarts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            _update_routine(service, value.routine_id, deleting=True)
            for choice in ("run", "recreate"):
                with self.subTest(choice=choice):
                    self.refused(service, run_id, choice, "routine-not-found")
            self.assertTrue(record.routine(self.state(service), value.routine_id).deleting)

    def test_rodar_refuses_a_routine_that_awaits_reconfirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            _update_routine(service, value.routine_id, needs_reconfirm=True)
            self.refused(service, run_id, "run", "routine-contracts-changed")
            self.assertEqual(record.routine(self.state(service), value.routine_id).run_requested, 0)


class RecriarRefusalTests(CardCase):
    def held_with(self, directory: str, *answers: object):
        return RecriarTests.held_with(self, directory, *answers)

    def test_a_compiled_create_or_question_that_cannot_stand_is_refused(self) -> None:
        uncited = _change()
        uncited["steps"][0]["input"]["per_page"] = {"kind": "literal", "value": 77, "origins": [_origin("77")]}
        cases = (
            # A change that does not parse, and a literal the person never wrote, are not admitted as their request.
            (_compiled({**_change(), "unexpected": 1}), None),
            (_compiled(uncited), None),
            # A question whose shape does not parse, one about another field, or one with no option the person chose.
            (_compiled(_asked(field={"kind": "nothing"}), CLARIFICATION), (("schedule",), DAILY)),
            (_compiled(_asked(), CLARIFICATION), (("timezone",), "UTC")),
            (_compiled(_asked(), CLARIFICATION), (("schedule",), {"kind": "daily", "time": "11:00"})),
        )
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, *(answer for answer, _selected in cases))
            for answer, selected in cases:
                with self.subTest(answer=answer["routine"].get("question")):
                    service.routine_store.delete_source("team_1", value.routine_id)
                    self.seal(service, value, selected=selected)
                    self.refused(service, run_id, "recreate", "routine-recreate-refused")
            self.assertEqual(record.routine(self.state(service), value.routine_id).revision, value.revision)
        self.assertEqual(len(brain.compiled), len(cases))

    def test_a_missing_or_switched_model_configuration_never_compiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory)
            self.seal(service, value)
            with mock.patch.object(
                service.inference_store, "load", side_effect=inference_config.InferenceConfigError("down")
            ):
                self.refused(service, run_id, "recreate", "routine-recreate-unavailable")
            self.refused(
                service, run_id, "recreate", "routine-recreate-unavailable", credential=("anthropic", "sk-ant-x")
            )
        self.assertEqual(brain.compiled, [])

    def test_a_team_whose_network_or_contracts_moved_is_refused_before_or_after_the_compile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, _compiled(_change()))
            self.seal(service, value)
            real = service._team_assistants

            def moved(after: int):
                calls = [0]

                def team_assistants(team_id):
                    calls[0] += 1
                    name, network_id, active = real(team_id)
                    return name, network_id if calls[0] <= after else "another-network", active

                return team_assistants

            with mock.patch.object(service, "_team_assistants", side_effect=moved(0)):
                self.refused(service, run_id, "recreate", "routine-card-stale")
            self.assertEqual(brain.compiled, [])
            # Recreate's own reads only: the first before the compile, the second under the lock before the commit.
            card = self.card(service, run_id)
            with (
                mock.patch.object(routine_recreate, "_contracts", wraps=routine_recreate._contracts) as contracts,
                mock.patch.object(service, "_team_assistants", side_effect=moved(1)),
                self.assertRaises(routine_card.ApiProblem) as changed,
            ):
                self.answer(service, run_id, card, "recreate")
            self.assertEqual((changed.exception.code, contracts.call_count), ("team-context-changed", 2))
            state = self.state(service)
        self.assertEqual(len(brain.compiled), 1)
        self.assertEqual([item.status for item in state.incidents], ["unresolved"])
        self.assertEqual(record.routine(state, value.routine_id).revision, value.revision)

    def test_a_request_whose_receipt_already_changed_a_routine_never_commits_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, _compiled(_change()))
            self.seal(service, value)
            card = self.card(service, run_id)
            network = service.assistant_lifecycle._network("team_1").id
            receipt = RoutineRequest(PRINCIPAL, MESSAGE, 0, card["nonce"]).receipt(network)
            service.routine_store.update(
                "team_1",
                lambda state: (dataclasses.replace(state, receipts=((receipt, int(time.time()) + 600),)), None),
            )
            with self.assertRaises(routine_card.ApiProblem) as replayed:
                self.answer(service, run_id, card, "recreate")
            state = self.state(service)
        self.assertEqual(replayed.exception.code, "routine-receipt-replayed")
        self.assertEqual(len(brain.compiled), 1)
        self.assertEqual([item.status for item in state.incidents], ["unresolved"])
        self.assertEqual(record.routine(state, value.routine_id).revision, value.revision)


class DeletionRaceTests(CardCase):
    def test_a_deletion_completed_on_a_stale_read_leaves_a_routine_that_is_no_longer_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, _run_id = self.held(directory, Assistant([failed()], []))
            current = self.state(service)
            # The read saw the Routine deleting with no run; by the write it is listed and idle again.
            deleting = dataclasses.replace(
                current,
                runs=(),
                incidents=(),
                routines=tuple(dataclasses.replace(item, deleting=True) for item in current.routines),
            )
            with mock.patch.object(routine_manage.routine_state, "load", return_value=deleting):
                self.assertFalse(routine_manage.complete_deletion(service, "team_1", value.routine_id))
            self.assertEqual([item.routine_id for item in self.state(service).routines], [value.routine_id])
