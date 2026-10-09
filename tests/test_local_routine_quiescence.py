"""An attempt Team never classified is verified only after its workload is proven stopped since (ADR-0092)."""

import dataclasses
import datetime
import tempfile
from types import SimpleNamespace
from unittest import mock

from docker.errors import DockerException
from test_local_routine_recovery import Assistant, RecoveryCase, failed

from local import app as local_app
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery


def instant(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class QuiescenceTests(RecoveryCase):
    def interrupted(self, directory: str):
        """A held run whose RPC was interrupted by a Team crash: dispatched to its workload, never classified."""
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}] * 4)
        service, _brain, value, run_id = self.held(directory, assistant)
        cursor = routine_incident.open_recovery(service, "team_1", run_id).cursor
        self.assertEqual(cursor.workload, "assistant-container")
        self.assertGreater(cursor.dispatched_at, 0)
        service.routine_store.put_cursor("team_1", dataclasses.replace(cursor, fault=""))
        return service, value, run_id, assistant, cursor.dispatched_at

    def test_an_interrupted_rpc_is_never_verified_while_its_workload_may_still_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, value, run_id, assistant, at = self.interrupted(directory)

            def container(state=None, identifier="assistant-container"):
                attrs = {} if state is None else {"State": state}
                return SimpleNamespace(id=identifier, reload=lambda: None, attrs=attrs)

            unproven = (
                container(),
                container({"Running": True, "StartedAt": instant(at - 60)}),
                container({"Running": True, "StartedAt": instant(at)}),
                container({"Running": True, "StartedAt": "not an instant"}),
                container({"Running": True}),
            )
            for current in unproven:
                with (
                    self.subTest(state=current.attrs),
                    mock.patch.object(service.assistant_lifecycle, "_assistant_container", return_value=current),
                ):
                    self.assertEqual(self.verify(service, value, run_id), "unquiesced")
            for problem in (DockerException("down"), local_app.ApiProblem(503, "x", code="docker-unavailable")):
                with (
                    self.subTest(problem=problem),
                    mock.patch.object(service.assistant_lifecycle, "_assistant_container", side_effect=problem),
                ):
                    self.assertEqual(self.verify(service, value, run_id), "unquiesced")
            with mock.patch.object(service.assistant_lifecycle, "_assistant_container", return_value=container()):
                card = self.card(service, run_id)
                # Nor may a person start the Routine again while it could: Rodar is refused and changes nothing.
                with self.as_person(), self.assertRaises(local_app.ApiProblem) as refused:
                    service.answer_routine_card("team_1", run_id, {"nonce": card["nonce"], "choice": "run"})
            self.assertEqual(refused.exception.code, "routine-workload-unquiesced")
            # No verifier ever ran while the original execution could still act.
            self.assertNotIn("find-record", [action for action, _id in assistant.calls])
            proven = (
                container({"Running": False}),
                container({"Running": True, "StartedAt": instant(at + 5)}),
            )
            gone = local_app.ApiProblem(404, "x", code="assistant-not-found")
            lookups = (
                *({"return_value": current} for current in proven),
                {"side_effect": gone},
                {"return_value": container({"Running": True}, "replaced")},
            )
            unclassified = routine_incident.open_recovery(service, "team_1", run_id).cursor
            for lookup in lookups:
                # Each proof is judged on the same unclassified, unverified attempt.
                service.routine_store.put_cursor("team_1", unclassified)
                with (
                    self.subTest(lookup=lookup),
                    mock.patch.object(service.assistant_lifecycle, "_assistant_container", **lookup),
                ):
                    verdict = self.verify(service, value, run_id)
                self.assertIn(verdict, {"absent", "inconclusive"})
        # Once its workload was proven stopped, the verifier ran.
        self.assertIn("find-record", [action for action, _id in assistant.calls])

    def test_an_attempt_with_no_recorded_workload_is_never_proven_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, value, run_id, _assistant, _at = self.interrupted(directory)
            cursor = routine_incident.open_recovery(service, "team_1", run_id).cursor
            service.routine_store.put_cursor("team_1", dataclasses.replace(cursor, workload=""))
            self.assertEqual(self.verify(service, value, run_id), "unquiesced")
        self.assertIsNone(routine_recovery._docker_instant("2026-10-02"))
        self.assertIsNone(routine_recovery._docker_instant(7))
        self.assertIsNone(routine_recovery._docker_instant("2026-13-40T99:00:00Z"))
        self.assertEqual(routine_recovery._docker_instant("1970-01-01T00:00:10Z"), 10)

    @staticmethod
    def as_person():
        from local import audit as local_audit

        return local_audit.bind_request_principal(local_audit.AuditPrincipal("a" * 32, "human"))

    def card(self, service, run_id: str) -> dict[str, object]:
        with self.as_person():
            return service.open_routine_card("team_1", run_id)
