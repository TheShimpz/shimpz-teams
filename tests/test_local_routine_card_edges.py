"""A held run's card refuses every stale, drifted, or unmatched Rodar and changes nothing (ADR-0092, ADR-0101)."""

import dataclasses
import tempfile
from types import SimpleNamespace
from unittest import mock

import routine_fixture
from test_local_routine_card import CardCase
from test_local_routine_recovery import Assistant, failed

from local.routine import manage as routine_manage
from routine import record


class CardBindingTests(CardCase):
    def test_a_diagnostic_recorded_for_another_step_is_shown_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            read = service.routine_diagnostics.read

            def elsewhere(change: dict[str, object]):
                # The held operation's own record, but naming another Action, or another position, than the held call.
                def read_changed(*args):
                    return [
                        SimpleNamespace(
                            operation_id=item.operation_id, view=lambda item=item: {**item.view(), **change}
                        )
                        for item in read(*args)
                    ]

                return read_changed

            self.assertEqual(self.card(service, run_id)["evidence"], "recorded")
            cards = []
            for change in ({"action": "x"}, {"position": {"phase": "replay", "step": 1}}):
                with mock.patch.object(service.routine_diagnostics, "read", side_effect=elsewhere(change)):
                    cards.append(self.card(service, run_id))
        self.assertEqual([(card["evidence"], card["diagnostic"]) for card in cards], [("absent", None)] * 2)

    def test_a_card_opened_while_the_routine_is_deleting_restarts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            routine_fixture.update_routine(service, value.routine_id, deleting=True)
            self.refused(service, run_id, "run", "routine-not-found")
            self.assertTrue(record.routine(self.state(service), value.routine_id).deleting)

    def test_rodar_refuses_a_routine_that_awaits_reconfirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            routine_fixture.update_routine(service, value.routine_id, needs_reconfirm=True)
            self.refused(service, run_id, "run", "routine-contracts-changed")
            self.assertEqual(record.routine(self.state(service), value.routine_id).run_requested, 0)


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
