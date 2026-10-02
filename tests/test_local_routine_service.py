"""Local Routine runs end to end on the Local controller: confirm, claim, run, freeze, resume, stop (ADR-0086)."""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_controller_harness import LocalContractCase
from test_local_chat_scope import LOOKUP_INPUT, LOOKUP_RESULT

from action import human as action_human
from inference import client as brain_runtime_client
from install.bindings import DynamicAssistantStore
from local import app as local_app
from local import audit as local_audit
from local import authority as local_authority
from local.install.registry import AssistantRegistry
from local.labels import ASSISTANT_LABEL
from local.routine import proposal as routine_proposal
from local.routine import run as routine_run
from local.routine import store as routine_store
from local.routine import turn as routine_turn
from local.routine import watchdog as routine_watchdog
from routine import record

KEY = "e" * 64
API_KEY = "sk-test-0123456789"
# SHA-256 of API_KEY, written out so tests check the fingerprint the boundary binds instead of recomputing it.
API_KEY_SHA256 = "0d3b560722915d2f931a4c4100a00ecbce063d121e577e6b93bbbe7c05f23ad6"
ASSISTANT = "shimpz-cloudflare"
CHANGE = {
    "op": "propose",
    "quote": "Every day at 9, list my zones",
    "schedule": {"kind": "daily", "time": "09:00"},
    "timezone": None,
    "routine_id": None,
}
LIST = brain_runtime_client.ActionRequest("action-1", ASSISTANT, "list-zones", LOOKUP_INPUT)


class Runtime:
    """A scripted Brain: each start or resume returns the next turn."""

    def __init__(self, *turns: brain_runtime_client.RuntimeTurn) -> None:
        self.turns = list(turns)
        self.contexts: list[brain_runtime_client.RuntimeContext] = []

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        return self.turns.pop(0)

    def resume(self, context, _results):
        self.contexts.append(context)
        return self.turns.pop(0)

    def delete_thread(self, _thread_id):
        return None

    def purpose(self, _context, _request, _assistant_name, _summary):
        return None


def completed(reply: str = "Your zones are listed.") -> brain_runtime_client.RuntimeTurn:
    return brain_runtime_client.RuntimeTurn("completed", reply, ())


def acting(*requests) -> brain_runtime_client.RuntimeTurn:
    return brain_runtime_client.RuntimeTurn("action-required", "", tuple(requests) or (LIST,))


def approval() -> action_human.HumanRequest:
    descriptor = {"kind": "approval", "ordinal": 0, "title": "List zones", "description": "Allow listing the zones."}
    descriptor["fingerprint"] = action_human._fingerprint(descriptor)
    return action_human.validate_request(descriptor, ("approval",))


class RoutineServiceCase(LocalContractCase):
    def setUp(self) -> None:
        super().setUp()
        for patch in (
            mock.patch.object(local_authority, "routine_key_fingerprint", return_value=KEY),
            mock.patch.object(local_audit, "record_request", return_value="a" * 32),
            mock.patch.object(local_audit, "record", return_value="a" * 32),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def service(self, directory: str, runtime: Runtime):
        controller = self._chat_controller(directory, runtime)
        service = controller.chat_turn_service
        service.routine_proposals = controller.routine_proposals = routine_proposal.ProposalBook()
        controller.brain_runtime = service.brain_runtime = runtime
        return controller, service

    def routine(self, service, *, next_run_at: int | None = None) -> record.Routine:
        """Add one confirmed daily Routine pinned to the Team's current contracts, due now unless told otherwise."""
        contracts = routine_turn.current_contracts(service, "team_1", (ASSISTANT,))
        value = record.Routine(
            routine_id=record.new_id(),
            quote=CHANGE["quote"],
            schedule=dict(CHANGE["schedule"]),
            timezone="UTC",
            assistants=tuple(sorted(contracts.items())),
            anchor=int(time.time()) - 3 * 86_400,
            next_run_at=0,
        )
        value = dataclasses.replace(value, next_run_at=record.next_after(value, value.anchor))
        service.routine_store.update("team_1", lambda state: (record.add_routine(state, value), None))
        due = int(time.time()) - 60 if next_run_at is None else next_run_at
        service.routine_store.update(
            "team_1",
            lambda state: (
                record._replace_routine(
                    state, dataclasses.replace(record.routine(state, value.routine_id), next_run_at=due)
                ),
                None,
            ),
        )
        return value

    def run_claim(self, service, claim: dict[str, object]) -> dict[str, object]:
        evidence = local_authority.RoutineEvidence(KEY, record.lease_sha256(claim["lease_token"]), "a" * 32, 0)
        return service.run_routine("team_1", claim["run_id"], evidence, "openai", API_KEY)

    def state(self, service) -> record.TeamRoutines:
        return service.routine_store.load("team_1")

    @contextmanager
    def broken_registry(self, service, directory: str, damage: str):
        """Read the Team's running Assistant through the real registry, whose store is unreadable or malformed."""
        path = Path(directory) / f"registry-{damage}" / "bindings.json"
        path.parent.mkdir()
        if damage.startswith("unreadable"):
            path.mkdir()
        else:
            path.write_bytes(b"not json")
        container = SimpleNamespace(id="assistant-container", status="running", labels={ASSISTANT_LABEL: ASSISTANT})
        docker = SimpleNamespace(containers=SimpleNamespace(list=lambda **_filters: [container]))
        # The real scan, not the harness's fixed answer, so the registry itself is read.
        scan = type(service)._active_chat_assistants.__get__(service)
        with (
            mock.patch.object(service, "_active_chat_assistants", scan),
            mock.patch.object(service, "registry", AssistantRegistry(DynamicAssistantStore(path))),
            mock.patch.object(service.assistant_lifecycle, "client", docker, create=True),
        ):
            yield


class ConfirmationTests(RoutineServiceCase):
    def test_a_proposal_previews_then_confirms_into_a_pinned_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            contracts = routine_turn.current_contracts(service, "team_1", (ASSISTANT,))
            proposal = service.routine_proposals.create("team_1", CHANGE, contracts)
            preview = service.preview_routine("team_1", proposal.proposal_id, {"timezone": "America/Sao_Paulo"})
            self.assertEqual(
                (preview["timezone"], len(preview["next_runs"]), preview["fits"]), ("America/Sao_Paulo", 3, True)
            )
            self.assertEqual((preview["daily_runs"], preview["max_daily_runs"]), ("1", 24))
            confirmed = service.confirm_routine(
                "team_1", {"proposal_id": proposal.proposal_id, "timezone": "America/Sao_Paulo"}
            )["routine"]
            self.assertEqual((confirmed["quote"], confirmed["assistant_ids"]), (CHANGE["quote"], [ASSISTANT]))
            listed = service.list_routines("team_1")
            self.assertEqual([item["routine_id"] for item in listed["routines"]], [confirmed["routine_id"]])
            self.assertEqual(self.state(service).routines[0].assistants, tuple(sorted(contracts.items())))
            # A proposal is one-use.
            with self.assertRaises(local_app.ApiProblem) as reused:
                service.confirm_routine("team_1", {"proposal_id": proposal.proposal_id, "timezone": "UTC"})
            self.assertEqual(reused.exception.code, "routine-proposal-unavailable")

    def test_confirmation_refuses_a_changed_scope_bad_input_and_a_full_team(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            stale = service.routine_proposals.create("team_1", CHANGE, {ASSISTANT: "sha256:" + "0" * 64})
            with self.assertRaises(local_app.ApiProblem) as changed:
                service.confirm_routine("team_1", {"proposal_id": stale.proposal_id, "timezone": "UTC"})
            self.assertEqual(changed.exception.code, "team-context-changed")
            contracts = routine_turn.current_contracts(service, "team_1", (ASSISTANT,))
            proposal = service.routine_proposals.create("team_1", CHANGE, contracts)
            for body, code in (
                ({"proposal_id": proposal.proposal_id}, "invalid-body"),
                ({"proposal_id": proposal.proposal_id, "timezone": "Mars/X"}, "invalid-timezone"),
            ):
                with self.subTest(code=code), self.assertRaises(local_app.ApiProblem) as refused:
                    service.confirm_routine("team_1", body)
                self.assertEqual(refused.exception.code, code)
            hourly = service.routine_proposals.create(
                "team_1", {**CHANGE, "schedule": {"kind": "hourly", "every": 1}}, contracts
            )
            service.confirm_routine("team_1", {"proposal_id": hourly.proposal_id, "timezone": "UTC"})
            full = service.routine_proposals.create("team_1", CHANGE, contracts)
            self.assertFalse(service.preview_routine("team_1", full.proposal_id, {"timezone": "UTC"})["fits"])
            with self.assertRaises(local_app.ApiProblem) as over:
                service.confirm_routine("team_1", {"proposal_id": full.proposal_id, "timezone": "UTC"})
            self.assertEqual(over.exception.code, "routine-rate-limit")

    def test_confirmation_is_retryable_while_the_team_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            contracts = routine_turn.current_contracts(service, "team_1", (ASSISTANT,))
            proposal = service.routine_proposals.create("team_1", CHANGE, contracts)
            down = local_app.ApiProblem(503, "Docker is unavailable", code="docker-unavailable")
            with (
                mock.patch.object(service, "_active_chat_assistants", side_effect=down),
                self.assertRaises(local_app.ApiProblem) as unavailable,
            ):
                service.confirm_routine("team_1", {"proposal_id": proposal.proposal_id, "timezone": "UTC"})
            self.assertEqual(
                (unavailable.exception.status, unavailable.exception.code), (503, "team-context-unavailable")
            )
            self.assertEqual(self.state(service).routines, ())
            # Neither a transient read failure nor a wrong timezone spends the one-use proposal.
            with self.assertRaises(local_app.ApiProblem) as invalid:
                service.confirm_routine("team_1", {"proposal_id": proposal.proposal_id, "timezone": "Mars/X"})
            self.assertEqual(invalid.exception.code, "invalid-timezone")
            confirmed = service.confirm_routine("team_1", {"proposal_id": proposal.proposal_id, "timezone": "UTC"})
            self.assertEqual(
                [item.routine_id for item in self.state(service).routines], [confirmed["routine"]["routine_id"]]
            )
            with self.assertRaises(local_app.ApiProblem) as spent:
                service.confirm_routine("team_1", {"proposal_id": proposal.proposal_id, "timezone": "UTC"})
            self.assertEqual(spent.exception.code, "routine-proposal-unavailable")

    def test_of_two_validated_confirmations_only_the_one_that_takes_the_proposal_creates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            contracts = routine_turn.current_contracts(service, "team_1", (ASSISTANT,))
            proposal = service.routine_proposals.create("team_1", CHANGE, contracts)
            current = routine_turn.current_contracts

            def taken_meanwhile(*args):
                result = current(*args)
                service.routine_proposals.take("team_1", proposal.proposal_id)
                return result

            with (
                mock.patch.object(routine_turn, "current_contracts", side_effect=taken_meanwhile),
                self.assertRaises(local_app.ApiProblem) as lost,
            ):
                service.confirm_routine("team_1", {"proposal_id": proposal.proposal_id, "timezone": "UTC"})
            self.assertEqual(lost.exception.code, "routine-proposal-unavailable")
            self.assertEqual(self.state(service).routines, ())

    def test_a_confirmed_cancellation_deletes_the_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            cancel = {
                "op": "cancel",
                "quote": "stop listing my zones",
                "schedule": None,
                "timezone": None,
                "routine_id": value.routine_id,
            }
            proposal = service.routine_proposals.create("team_1", cancel, {})
            preview = service.preview_routine("team_1", proposal.proposal_id, {"timezone": "UTC"})
            self.assertEqual((preview["next_runs"], preview["fits"]), ([], True))
            deleted = service.confirm_routine("team_1", {"proposal_id": proposal.proposal_id, "timezone": "UTC"})
            self.assertTrue(deleted["deleted"])
            self.assertEqual(self.state(service).routines, ())
            for routine_id in ("0" * 32, 7):
                with self.subTest(routine_id=routine_id), self.assertRaises(local_app.ApiProblem) as missing:
                    service.delete_routine("team_1", routine_id)
                self.assertEqual(missing.exception.code, "routine-not-found")


class RunTests(RoutineServiceCase):
    def test_a_due_routine_is_claimed_run_and_delivered_as_done(self) -> None:
        runtime = Runtime(acting(), completed())
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, runtime)
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            result = self.run_claim(service, claim)
            state = self.state(service)
        self.assertEqual(result["status"], "done")
        self.assertEqual(state.runs, ())
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices], [("done", {"reply": "Your zones are listed."})]
        )
        context = runtime.contexts[0]
        self.assertEqual((context.knowledge_writable, context.routines), (False, None))
        self.assertTrue(context.thread_id.endswith(f":routine-{claim['run_id']}"))

    def test_nothing_is_claimed_without_a_routine_key_or_while_chat_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            with mock.patch.object(
                local_authority, "routine_key_fingerprint", side_effect=local_authority.SupervisorUnavailableError
            ):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            lock = service._chat_lock("team_1")
            lock.acquire()
            try:
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            finally:
                lock.release()
            self.assertIsNotNone(service.claim_routine_run(("anthropic", "openai")))

    def test_a_changed_assistant_contract_marks_the_routine_for_reconfirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            with mock.patch.object(routine_turn, "current_contracts", return_value={ASSISTANT: "sha256:" + "0" * 64}):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            state = self.state(service)
        self.assertTrue(record.routine(state, value.routine_id).needs_reconfirm)
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices], [("scope-changed", {"assistants": [ASSISTANT]})]
        )

    def test_unreadable_contracts_never_mark_the_routine_changed(self) -> None:
        down = local_app.ApiProblem(503, "Docker is unavailable", code="docker-unavailable")
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            with mock.patch.object(service, "_active_chat_assistants", side_effect=down):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            state = self.state(service)
            self.assertEqual((record.routine(state, value.routine_id).needs_reconfirm, state.notices), (False, ()))
            # The claim reads the contracts; the second read, in the run's slot, fails.
            claim = service.claim_routine_run(("anthropic", "openai"))
            with mock.patch.object(service, "_active_chat_assistants", side_effect=down):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            state = self.state(service)
        self.assertFalse(record.routine(state, value.routine_id).needs_reconfirm)
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices],
            [("failed", {"code": "team-context-unavailable", "actions": []})],
        )

    def test_an_unreadable_or_malformed_registry_never_marks_the_routine_changed_or_strands_a_run(self) -> None:
        for damage in ("unreadable", "malformed"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                _controller, service = self.service(directory, Runtime())
                value = self.routine(service)
                with self.broken_registry(service, directory, damage):
                    self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
                state = self.state(service)
                self.assertEqual((record.routine(state, value.routine_id).needs_reconfirm, state.notices), (False, ()))
                claim = service.claim_routine_run(("anthropic", "openai"))
                with self.broken_registry(service, directory, f"{damage}-run"):
                    self.assertEqual(self.run_claim(service, claim)["status"], "failed")
                state = self.state(service)
                self.assertEqual(state.runs, ())
                self.assertFalse(record.routine(state, value.routine_id).needs_reconfirm)
                self.assertEqual(state.notices[-1].detail, {"code": "team-context-unavailable", "actions": []})

    def test_an_assistant_the_team_no_longer_runs_marks_the_routine_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            with mock.patch.object(service, "_active_chat_assistants", return_value=()):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            state = self.state(service)
        self.assertTrue(record.routine(state, value.routine_id).needs_reconfirm)
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices], [("scope-changed", {"assistants": [ASSISTANT]})]
        )

    def test_a_question_ends_the_run_as_needs_input(self) -> None:
        question = {
            "question": "Which zone?",
            "options": [{"label": "a", "description": ""}, {"label": "b", "description": ""}],
            "default_index": 0,
        }
        runtime = Runtime(
            brain_runtime_client.RuntimeTurn("completed", "Which zone?\n\n1. a ✓\n2. b", (), clarification=question)
        )
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, runtime)
            self.routine(service)
            result = self.run_claim(service, service.claim_routine_run(("anthropic", "openai")))
            notices = self.state(service).notices
        self.assertEqual(result["status"], "needs-input")
        self.assertEqual(notices[0].detail, {"question": "Which zone?"})

    def test_a_failing_action_is_held_uncertain_until_a_supervisor_resolves_its_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime(acting()))

            def failing(*_args):
                raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-rpc-failed")

            controller.assistant_lifecycle.invoke = failing
            value = self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            result = self.run_claim(service, claim)
            held = record.run(self.state(service), claim["run_id"])
            self.assertEqual((result["status"], held.status), ("uncertain", "uncertain"))
            self.assertEqual(self.state(service).notices[0].detail, {"actions": [[ASSISTANT, "list-zones"]]})
            # Delivery never releases it; only the exact batch's resolution does.
            service.acknowledge_routine_notices(
                {
                    "deliveries": [
                        {"team_id": "team_1", "notice_id": item.notice_id, "version": item.version}
                        for item in self.state(service).notices
                    ]
                }
            )
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "uncertain")
            with self.assertRaises(local_app.ApiProblem) as wrong:
                service.resolve_routine_run("team_1", claim["run_id"], {"batch_fingerprint": "0" * 64})
            self.assertEqual(wrong.exception.code, "routine-run-not-uncertain")
            with self.assertRaises(local_app.ApiProblem) as stop:
                service.stop_routine("team_1", claim["run_id"])
            self.assertEqual(stop.exception.code, "routine-run-uncertain")
            # A confirmed cancellation the uncertain run refuses keeps its one-use offer for after the resolution.
            cancel = {
                "op": "cancel",
                "quote": "stop listing my zones",
                "schedule": None,
                "timezone": None,
                "routine_id": value.routine_id,
            }
            offer = {
                "proposal_id": service.routine_proposals.create("team_1", cancel, {}).proposal_id,
                "timezone": "UTC",
            }
            with self.assertRaises(local_app.ApiProblem) as refused:
                service.confirm_routine("team_1", offer)
            self.assertEqual(refused.exception.code, "routine-run-uncertain")
            self.assertFalse(record.routine(self.state(service), value.routine_id).deleting)
            service.resolve_routine_run("team_1", claim["run_id"], {"batch_fingerprint": held.batch[1]})
            self.assertEqual(self.state(service).runs, ())
            self.assertIsNone(controller.action_state.uncertain_fingerprint(held.generation))
            self.assertEqual(record.routine(self.state(service), value.routine_id).routine_id, value.routine_id)
            # An offer spent meanwhile admits nothing: the Routine is not even marked deleting.
            with (
                mock.patch.object(
                    service.routine_proposals, "take", side_effect=routine_proposal.ProposalError("used")
                ),
                self.assertRaises(local_app.ApiProblem) as spent,
            ):
                service.confirm_routine("team_1", offer)
            self.assertEqual(spent.exception.code, "routine-proposal-unavailable")
            self.assertFalse(record.routine(self.state(service), value.routine_id).deleting)
            self.assertTrue(service.confirm_routine("team_1", offer)["deleted"])
            self.assertEqual(self.state(service).routines, ())
            with self.assertRaises(local_app.ApiProblem) as reused:
                service.confirm_routine("team_1", offer)
            self.assertEqual(reused.exception.code, "routine-proposal-unavailable")

    def test_other_failures_and_a_dead_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            with mock.patch.object(
                service, "_run_chat_segment", side_effect=local_app.ApiProblem(409, "stopped", code="chat-stopped")
            ):
                self.assertEqual(self.run_claim(service, claim)["status"], "stopped")
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            with mock.patch.object(
                service, "_run_chat_segment", side_effect=local_app.ApiProblem(409, "x", code="team-context-changed")
            ):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            with self.assertRaises(local_app.ApiProblem) as dead:
                self.run_claim(service, claim)
            self.assertEqual(dead.exception.code, "routine-lease-invalid")


class FreezeTests(RoutineServiceCase):
    def paused(self, directory: str, *turns):
        runtime = Runtime(acting(), *turns)
        controller, service = self.service(directory, runtime)
        calls: list[object] = []

        def invoke(*args):
            calls.append(args)
            if len(calls) == 1:
                raise action_human.HumanRequestSuspensionError(approval())
            return {"result": LOOKUP_RESULT}

        controller.assistant_lifecycle.invoke = invoke
        self.routine(service)
        claim = service.claim_routine_run(("anthropic", "openai"))
        return controller, service, claim, self.run_claim(service, claim)

    def test_an_approval_freezes_the_run_and_a_person_resumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, frozen = self.paused(directory, completed("Approved and listed."))
            self.assertEqual(frozen["status"], "frozen")
            run = record.run(self.state(service), claim["run_id"])
            self.assertEqual((run.status, run.request_kind, run.action), ("frozen", "human", "list-zones"))
            # A frozen run never blocks chat: no chat challenge exists.
            self.assertIsNone(service.human_challenges.current("team_1"))
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            self.assertEqual(opened["run_id"], claim["run_id"])
            resumed = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                "openai",
                API_KEY,
            )
            state = self.state(service)
        self.assertEqual(resumed["status"], "done")
        self.assertEqual((state.runs, state.notices[-1].detail), ((), {"reply": "Approved and listed."}))

    def test_a_rename_never_ends_a_frozen_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service, claim, _frozen = self.paused(directory, completed("Approved and listed."))
            # Only the display name changes; the run's Team context keeps the immutable creation label.
            controller.team_names.save("team_1", "a" * 64, "Growth")
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            resumed = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                "openai",
                API_KEY,
            )
        self.assertEqual(resumed["status"], "done")

    def test_an_answer_refused_while_a_chat_holds_the_team_stays_answerable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory, completed("Approved and listed."))
            run_id = claim["run_id"]
            opened = service.open_routine_challenge("team_1", run_id)
            holding, release = threading.Event(), threading.Event()

            def chat() -> None:
                with service._exclusive_chat_turn("team_1"):
                    holding.set()
                    release.wait(10)

            worker = threading.Thread(target=chat)
            worker.start()
            try:
                self.assertTrue(holding.wait(10))
                for answer in ({"decision": "deny"}, {"decision": "submit", "value": True}):
                    with self.subTest(answer=answer), self.assertRaises(local_app.ApiProblem) as busy:
                        service.resume_routine_human(
                            "team_1", run_id, {"challenge_id": opened["challenge_id"], **answer}, "openai", API_KEY
                        )
                    self.assertEqual(busy.exception.code, "chat-active")
                    # Nothing was consumed: the run, its continuation, and its challenge wait for the same answer.
                    self.assertEqual(record.run(self.state(service), run_id).status, "frozen")
                    self.assertEqual(service.routine_store.continuations("team_1"), (run_id,))
                    self.assertEqual(service.current_routine_challenge("team_1").id, opened["challenge_id"])
            finally:
                release.set()
                worker.join()
            resumed = service.resume_routine_human(
                "team_1",
                run_id,
                {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                "openai",
                API_KEY,
            )
            self.assertEqual(resumed["status"], "done")
            self.assertIsNone(service.current_routine_challenge("team_1"))
            self.assertEqual(self.state(service).runs, ())

    def test_a_denied_or_stopped_frozen_run_ends_and_an_expired_challenge_leaves_it_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            with self.assertRaises(local_app.ApiProblem) as expired:
                service.resume_routine_human(
                    "team_1", claim["run_id"], {"challenge_id": "0" * 32, "decision": "deny"}, "openai", API_KEY
                )
            self.assertEqual(expired.exception.code, "human-request-expired")
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "frozen")
            denied = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "deny"},
                "openai",
                API_KEY,
            )
            self.assertEqual((denied["status"], self.state(service).notices[-1].outcome), ("denied", "denied"))
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            service.open_routine_challenge("team_1", claim["run_id"])
            self.assertTrue(service.stop_routine("team_1", claim["run_id"])["stopped"])
            self.assertIsNone(service.current_routine_challenge("team_1"))
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")

    def test_invalid_answers_and_runs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            cases = (
                ({"decision": "submit"}, "invalid-body"),
                (
                    {"challenge_id": opened["challenge_id"], "decision": "submit", "value": "yes"},
                    "invalid-human-response",
                ),
            )
            for body, code in cases:
                with self.subTest(code=code), self.assertRaises(local_app.ApiProblem) as refused:
                    service.resume_routine_human("team_1", claim["run_id"], body, "openai", API_KEY)
                self.assertEqual(refused.exception.code, code)
            with self.assertRaises(local_app.ApiProblem) as provider:
                service.resume_routine_human(
                    "team_1",
                    claim["run_id"],
                    {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                    "anthropic",
                    API_KEY,
                )
            self.assertEqual(provider.exception.code, "inference-provider-mismatch")
            for run_id, code in (("0" * 32, "routine-run-not-found"), (7, "routine-run-not-found")):
                with self.subTest(run_id=run_id), self.assertRaises(local_app.ApiProblem) as missing:
                    service.open_routine_challenge("team_1", run_id)
                self.assertEqual(missing.exception.code, code)
            with self.assertRaises(local_app.ApiProblem) as integration:
                service.resume_routine_integrations("team_1", claim["run_id"], "openai", API_KEY)
            self.assertEqual(integration.exception.code, "routine-run-not-frozen")

    def test_a_changed_team_ends_the_frozen_run_instead_of_replaying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            with (
                mock.patch.object(service, "_chat_identity", return_value=("changed",)),
                self.assertRaises(local_app.ApiProblem) as changed,
            ):
                service.open_routine_challenge("team_1", claim["run_id"])
            self.assertEqual(changed.exception.code, "team-context-changed")
            self.assertEqual(self.state(service).notices[-1].detail["code"], "team-context-changed")


class NoticeAndWatchdogTests(RoutineServiceCase):
    def test_notices_are_listed_for_every_team_and_acknowledged_by_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime(completed()))
            self.routine(service)
            self.run_claim(service, service.claim_routine_run(("anthropic", "openai")))
            notices = service.routine_notices()["notices"]
            self.assertEqual(
                [(item["team_id"], item["outcome"], item["version"]) for item in notices], [("team_1", "done", 1)]
            )
            for body in ({}, {"deliveries": []}, {"deliveries": [{"team_id": "team_1"}]}):
                with self.subTest(body=body), self.assertRaises(local_app.ApiProblem):
                    service.acknowledge_routine_notices(body)
            service.acknowledge_routine_notices(
                {"deliveries": [{"team_id": "team_1", "notice_id": notices[0]["notice_id"], "version": 2}]}
            )
            self.assertEqual(len(service.routine_notices()["notices"]), 1)
            service.acknowledge_routine_notices(
                {"deliveries": [{"team_id": "team_1", "notice_id": notices[0]["notice_id"], "version": 1}]}
            )
            self.assertEqual(service.routine_notices()["notices"], [])

    def test_the_watchdog_recovers_orphaned_runs_and_stops_expired_ones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            orphan = service.claim_routine_run(("anthropic", "openai"))
            service.routine_store.put_continuation("team_1", "f" * 32, b"orphaned continuation")
            routine_watchdog.check(service, startup=True)
            state = self.state(service)
            self.assertEqual((state.runs, state.notices[-1].detail), ((), {"code": "interrupted", "actions": []}))
            self.assertEqual(service.routine_store.continuations("team_1"), ())
            self.routine(service)
            running = service.claim_routine_run(("anthropic", "openai"))
            routine_run.register_routine_run(service, "team_1", running["run_id"], "token", 600)
            service._active_chat_tokens["team_1"] = "token"
            with mock.patch.object(
                record, "expired", return_value=(record.run(self.state(service), running["run_id"]),)
            ):
                routine_watchdog.check(service)
            self.assertIn("token", service._cancelled_chat_tokens)
        self.assertIsNotNone(orphan)

    def test_a_recovered_run_whose_batch_may_have_acted_is_held_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            network = controller.assistant_lifecycle._network("team_1").id
            lease = record.lease_of(claim["lease_token"], KEY)
            service.routine_store.update(
                "team_1",
                lambda state: (record.bind_generation(state, claim["run_id"], lease, int(time.time()), network), None),
            )
            generation = record.generation_for(network, claim["run_id"])
            operation = local_app.action_journal.Operation("action-1", "b" * 64)
            batch = controller.action_state.prepare_batch(generation, "thread", (operation,))
            controller.action_state.begin(batch, operation)
            routine_watchdog.check(service, startup=True)
            held = record.run(self.state(service), claim["run_id"])
        self.assertEqual((held.status, held.batch[1]), ("uncertain", batch.fingerprint))

    def test_the_watchdog_survives_a_failed_audit_and_runs_its_next_pass(self) -> None:
        second_pass = threading.Event()
        passes = []

        def teams():
            passes.append(1)
            if len(passes) >= 2:
                second_pass.set()
            raise routine_store.RoutineStoreError("down")

        service = SimpleNamespace(
            _active_chat_guard=threading.Lock(),
            _routine_runs={},
            routine_store=SimpleNamespace(teams=teams),
        )
        local_audit.record.side_effect = RuntimeError("the local audit journal could not be synchronized")
        watchdog = routine_watchdog.RoutineWatchdog(service, interval=0.01)
        with self.assertLogs(routine_watchdog.log, "ERROR") as logs:
            watchdog.start()
            self.assertTrue(second_pass.wait(5))
            self.assertTrue(watchdog._thread.is_alive())
            watchdog.close()
        self.assertIn("could not audit routine-watchdog/check-failed (RuntimeError)", logs.output[0])
        self.assertNotIn("synchronized", "".join(logs.output))

    def test_a_failed_recovery_audit_never_stops_the_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            service.claim_routine_run(("anthropic", "openai"))
            service.routine_store.put_continuation("team_1", "f" * 32, b"orphaned continuation")
            local_audit.record.side_effect = RuntimeError("the local audit journal could not be written")
            with self.assertLogs(routine_watchdog.log, "ERROR") as logs:
                routine_watchdog.check(service, startup=True)
            self.assertEqual(self.state(service).runs, ())
            self.assertEqual(service.routine_store.continuations("team_1"), ())
        self.assertIn("could not audit routine-recover/failed (RuntimeError)", logs.output[0])

    def test_a_failed_watchdog_pass_is_audited_and_retried(self) -> None:
        service = SimpleNamespace(
            _active_chat_guard=threading.Lock(),
            _routine_runs={},
            routine_store=SimpleNamespace(teams=mock.Mock(side_effect=routine_store.RoutineStoreError("down"))),
        )
        watchdog = routine_watchdog.RoutineWatchdog(service, interval=0.01)
        watchdog.start()
        time.sleep(0.05)
        watchdog.close()
        self.assertGreaterEqual(service.routine_store.teams.call_count, 1)
        local_audit.record.assert_any_call(
            "routine-watchdog",
            result="error",
            principal=routine_watchdog._PRINCIPAL,
            team_id=None,
            detail="check-failed",
        )
        self.assertIs(routine_run._problem(409, "x", "y").code, "y")
