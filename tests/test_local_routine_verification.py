"""A held run's verification generation goes with the run's own cleanup, which stays queued until it is removed."""

import tempfile
import unittest
from unittest import mock

import routine_fixture
from test_local_routine_incident import IncidentCase, _operation

from action import journal as action_journal
from local import app as local_app
from local.routine import incident as routine_incident
from local.routine import lifecycle as routine_lifecycle
from local.routine import manage as routine_manage
from routine import claim as routine_claim
from routine import record


class VerificationResidueTests(IncidentCase):
    def left_verification(self, service, run_id: str, generation: str) -> str:
        """The ended batch a held run's verification left in its own generation when its release failed."""
        verification = routine_claim.verification_of(generation, run_id)
        prepared = service.action_state.prepare_batch(verification, "thread", (_operation("verify"),))
        service.action_state.end(prepared)
        self.assertIsNotNone(service.action_state.current_batch(verification))
        return verification

    def failing_on(self, service, generation: str):
        real = service.action_state.purge

        def purge(value):
            if value == generation:
                raise action_journal.ActionJournalError("crash")
            return real(value)

        return mock.patch.object(service.action_state, "purge", side_effect=purge)

    def test_a_released_incident_keeps_its_cleanup_until_its_verification_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, generation, batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            verification = self.left_verification(service, run_id, generation)
            with self.failing_on(service, verification):
                routine_fixture.set_aside(service, "team_1", run_id)
            # The set-aside run stays indexed for the next pass, its verification's batch with it.
            self.assertEqual([item.status for item in record.incidents(self.state(service))], ["skipped"])
            self.assertIsNotNone(service.action_state.current_batch(verification))
            routine_incident.reconcile_team(service, "team_1")
            self.assertEqual(record.incidents(self.state(service)), ())
            self.assertIsNone(service.action_state.current_batch(verification))
            del batch

    def test_the_teams_deletion_keeps_its_state_until_every_verification_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service, _value, run_id, lease, generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            verification = self.left_verification(service, run_id, generation)
            with self.failing_on(service, verification), self.assertRaises(local_app.ApiProblem):
                routine_lifecycle._delete_team_routines(controller, "team_1")
            self.assertEqual(record.run(self.state(service), run_id).run_id, run_id)
            routine_lifecycle._delete_team_routines(controller, "team_1")
            self.assertIsNone(service.action_state.current_batch(verification))
            self.assertEqual(service.routine_store.load("team_1"), record.TeamRoutines())

    def test_an_unresolved_incidents_drain_keeps_its_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            verification = self.left_verification(service, run_id, generation)
            routine_manage.drain(service, "team_1")
            # Only the run's removal for good removes its verification: an unresolved incident may still verify.
            self.assertIsNotNone(service.action_state.current_batch(verification))

    def test_a_run_that_ended_for_good_removes_its_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            verification = self.left_verification(service, run_id, generation)
            routine_manage._discard(service, "team_1", run_id, generation, incident=False, live=False)
            self.assertIsNone(service.action_state.current_batch(verification))


if __name__ == "__main__":
    unittest.main()
