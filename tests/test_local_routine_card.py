"""A held run's card: its recorded failure, Rodar, and Recriar (ADR-0092 section 7, amended 2026-10-02)."""

from __future__ import annotations

import dataclasses
import tempfile
import time
from unittest import mock

import routine_fixture
from docker.errors import DockerException
from test_local_routine_compiled import Brain
from test_local_routine_recovery import Assistant, RecoveryCase, failed
from test_local_routine_service import ASSISTANT

from inference import client as brain_runtime_client
from local import app as local_app
from local import audit as local_audit
from local.routine import card as routine_card
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from local.routine import recreate as routine_recreate
from local.routine import run as routine_run
from local.routine import source as routine_source
from local.routine import turn as routine_turn
from local.routine import watchdog as routine_watchdog
from protocol.http.v1 import routine as http_routine
from routine import record
from routine import request as routine_request

PRINCIPAL = "a" * 32
CREDENTIAL = ("openai", "sk-test-0123456789")
MESSAGE = "Every day at 9, list my zones, page 1 with 25 per page.\n> and delete the old ones"
DAILY = {"kind": "daily", "time": "09:00"}


def _origin(text: str) -> dict[str, object]:
    return {"at": "", "from": "message", "text": text, "region": None, "instruction": None}


def _change(**changes: object) -> dict[str, object]:
    """The create Brain compiles from the creation message: one step, every literal cited from the person's words."""
    value = {
        "op": "create",
        "routine_id": None,
        "expected_revision": None,
        "continues": False,
        "name": "Daily zones",
        "request": "Every day at 9, list my zones",
        "schedule": DAILY,
        "timezone": None,
        "steps": [
            {
                "id": "zones",
                "assistant": ASSISTANT,
                "action": "list-zones",
                "input": {
                    "page": {"kind": "literal", "value": 1, "origins": [_origin("1")]},
                    "per_page": {"kind": "literal", "value": 25, "origins": [_origin("25")]},
                },
            }
        ],
    }
    value.update(changes)
    return value


class Compiler(Brain):
    """The Brain for Recriar: each compile answers in turn; anything else it is asked is a test failure."""

    def __init__(self, *answers: object) -> None:
        super().__init__()
        self.answers = list(answers)
        self.compiled: list[tuple[dict[str, object], str, str]] = []

    def routine_compile(self, payload, provider, model):
        self.compiled.append((payload, provider, model))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _compiled(routine=None, clarification=None, refusal=None) -> dict[str, object]:
    reply = None if refusal is not None else "Pronto."
    return {"routine": routine, "reply": reply, "clarification": clarification, "refusal": refusal}


class CardCase(RecoveryCase):
    @staticmethod
    def as_person(principal: str = PRINCIPAL):
        return local_audit.bind_request_principal(local_audit.AuditPrincipal(principal, "human"))

    def card(self, service, run_id: str) -> dict[str, object]:
        with self.as_person():
            card = service.open_routine_card("team_1", run_id)
        # Every card and answer Team produces is in its closed protocol view.
        self.assertEqual(http_routine.canonical_card(card), card)
        return card

    def answer(self, service, run_id: str, card, choice: str, principal: str = PRINCIPAL, credential=None):
        credential = credential if credential is not None or choice != "recreate" else CREDENTIAL
        with self.as_person(principal):
            answered = service.answer_routine_card(
                "team_1", run_id, {"nonce": card["nonce"], "choice": choice}, credential
            )
        self.assertEqual(http_routine.canonical_card_answer(answered), answered)
        return answered

    def refused(self, service, run_id: str, choice: str, code: str, **changes) -> None:
        with self.assertRaises(local_app.ApiProblem) as caught:
            self.answer(service, run_id, self.card(service, run_id), choice, **changes)
        self.assertEqual(caught.exception.code, code)
        # Nothing changed: the run is still held, its Routine at the same revision, and a fresh card opens.
        state = self.state(service)
        self.assertEqual([item.status for item in state.incidents], ["unresolved"])
        self.card(service, run_id)

    @staticmethod
    def seal_answer(service, value: record.Routine, label: str, selected: tuple) -> None:
        """Seal the words a bound answer seals: the asking message, then the label the person selected."""
        network = service.assistant_lifecycle._network("team_1").id
        source = routine_source.Source(value.routine_id, network, (("said", MESSAGE), ("said", label)), selected)
        routine_source.seal(service, "team_1", source)

    @staticmethod
    def seal(service, value: record.Routine, message: str = MESSAGE, selected=None, earlier=()) -> None:
        network = service.assistant_lifecycle._network("team_1").id
        parts = (*(("cited", text) for text in earlier), ("said", message))
        source = routine_source.Source(value.routine_id, network, parts, selected)
        routine_source.seal(service, "team_1", source)


class CardViewTests(CardCase):
    def test_a_card_names_the_held_step_its_recorded_failure_and_exactly_three_choices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held(directory, Assistant([failed()], []))
            (incident,) = service.list_routines("team_1")["incidents"]
            card = self.card(service, run_id)
        self.assertEqual(
            (card["choices"], card["assistant_id"], card["action"], card["revision"], card["step"], card["steps"]),
            (["run", "recreate", "delete"], ASSISTANT, "create-record", value.revision, 2, 2),
        )
        self.assertNotIn("recommended", card)
        failure = card["diagnostic"]["failure"]
        self.assertEqual(
            (card["evidence"], failure["error_type"], failure["http_status"], failure["provider"]),
            ("recorded", "HTTPStatusError", 404, "api.cloudflare.com"),
        )
        self.assertEqual(http_routine.canonical_incident_view(incident), incident)
        self.assertEqual(brain.calls, [])

    def test_an_unreadable_or_missing_diagnostic_is_said_never_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            with mock.patch.object(
                service.routine_diagnostics, "read", side_effect=routine_card.routine_diagnostics.DiagnosticStoreError
            ):
                unavailable = self.card(service, run_id)
            with mock.patch.object(service.routine_diagnostics, "read", return_value=()):
                absent = self.card(service, run_id)
        self.assertEqual((unavailable["evidence"], unavailable["diagnostic"]), ("unavailable", None))
        self.assertEqual((absent["evidence"], absent["diagnostic"]), ("absent", None))

    def test_an_answer_must_match_its_person_nonce_expiry_binding_and_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            cases = (
                (lambda card: self.answer(service, run_id, {**card, "nonce": "0" * 32}, "run"), "routine-card-expired"),
                (lambda card: self.answer(service, run_id, card, "run", principal="b" * 32), "routine-card-expired"),
                # Excluir and the retired choices are never card answers.
                (lambda card: self.answer(service, run_id, card, "delete"), "invalid-body"),
                (lambda card: self.answer(service, run_id, card, "verify"), "invalid-body"),
                (lambda card: self.answer(service, run_id, card, "skip"), "invalid-body"),
                (lambda card: self.answer(service, run_id, card, "pause"), "invalid-body"),
                # Rodar runs no model and carries no credential; Recriar always carries one.
                (
                    lambda card: self.answer(service, run_id, card, "run", credential=CREDENTIAL),
                    "routine-card-credential-invalid",
                ),
            )
            for attempt, code in cases:
                with self.subTest(code=code), self.assertRaises(local_app.ApiProblem) as caught:
                    attempt(self.card(service, run_id))
                self.assertEqual(caught.exception.code, code)
            with self.as_person(), self.assertRaises(local_app.ApiProblem) as missing:
                service.answer_routine_card(
                    "team_1", run_id, {"nonce": self.card(service, run_id)["nonce"], "choice": "recreate"}
                )
            self.assertEqual(missing.exception.code, "routine-card-credential-invalid")
            # Expired after five minutes.
            clock = [1000.0]
            service.routine_cards = routine_card.CardBook(now=lambda: clock[0])
            card = self.card(service, run_id)
            clock[0] += routine_card.CARD_SECONDS
            with self.assertRaises(local_app.ApiProblem) as expired:
                self.answer(service, run_id, card, "run")
            self.assertEqual(expired.exception.code, "routine-card-expired")
            # A Routine updated since the card opened makes it stale, as does a creation source sealed since.
            card = self.card(service, run_id)
            self.seal(service, value)
            with self.assertRaises(local_app.ApiProblem) as resealed:
                self.answer(service, run_id, card, "run")
            self.assertEqual(resealed.exception.code, "routine-card-stale")
            card = self.card(service, run_id)
            service.routine_store.update(
                "team_1",
                lambda state: (
                    record._replace_routine(
                        state,
                        routine_fixture.granted(
                            dataclasses.replace(record.routine(state, value.routine_id), revision=2)
                        ),
                    ),
                    None,
                ),
            )
            with self.assertRaises(local_app.ApiProblem) as stale:
                self.answer(service, run_id, card, "run")
            self.assertEqual(stale.exception.code, "routine-card-stale")
            with self.assertRaises(local_app.ApiProblem) as nobody:
                service.open_routine_card("team_1", run_id)
            self.assertEqual(nobody.exception.code, "routine-card-person-required")
            with self.as_person(), self.assertRaises(local_app.ApiProblem) as unknown:
                service.open_routine_card("team_1", "0" * 32)
            self.assertEqual(unknown.exception.code, "routine-incident-unavailable")


class RodarTests(CardCase):
    def test_rodar_sets_the_run_aside_and_one_fresh_run_starts_under_the_normal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held(directory, Assistant([failed()], []))
            # Recovery paused the Routine; Rodar is the person's decision to run it anyway.
            routine_incident.pause(service, "team_1", run_id, "exhausted")
            before = record.routine(self.state(service), value.routine_id)
            answered = self.answer(service, run_id, self.card(service, run_id), "run")
            state = self.state(service)
            requested = record.routine(state, value.routine_id)
            claim = service.claim_routine_run()
            claimed = self.state(service)
        self.assertEqual(answered["status"], "requested")
        self.assertEqual([item.status for item in state.incidents], ["released"])
        notice = next(item for item in state.notices if item.notice_id == run_id)
        self.assertEqual((notice.outcome, notice.detail["choice"]), ("user-skipped", "run"))
        self.assertEqual((requested.paused, requested.failures, requested.next_run_at), (False, 0, before.next_run_at))
        self.assertGreater(requested.run_requested, 0)
        # The fresh run is a new run with new operations; the standing cadence is unchanged.
        self.assertIsNotNone(claim)
        self.assertNotEqual(claim["run_id"], run_id)
        self.assertEqual(record.routine(claimed, value.routine_id).run_requested, 0)
        self.assertEqual(record.routine(claimed, value.routine_id).next_run_at, before.next_run_at)
        self.assertEqual(brain.calls, [])

    def test_rodar_and_recriar_refuse_what_could_overlap_or_drift_and_change_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            self.seal(service, value)
            with mock.patch.object(routine_recovery, "workload_stopped", return_value=False):
                self.refused(service, run_id, "run", "routine-workload-unquiesced")
                self.refused(service, run_id, "recreate", "routine-workload-unquiesced")
            with mock.patch.object(routine_turn, "current_contracts", return_value={}):
                self.refused(service, run_id, "run", "routine-contracts-changed")
            with mock.patch.object(
                routine_turn, "current_contracts", side_effect=routine_turn.ContractsUnavailableError
            ):
                self.refused(service, run_id, "run", "team-context-unavailable")
            busy = record.Run(
                record.new_id(),
                value.routine_id,
                "frozen",
                0,
                request_kind="human",
                assistant_id=ASSISTANT,
                action="list-zones",
            )
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=(busy,)), None))
            self.refused(service, run_id, "run", "routine-busy")
            self.refused(service, run_id, "recreate", "routine-busy")
            self.assertEqual(record.routine(self.state(service), value.routine_id).revision, value.revision)


class RecriarTests(CardCase):
    def held_with(self, directory: str, *answers: object):
        service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
        brain = Compiler(*answers)
        service.brain_runtime = brain
        return service, brain, value, run_id

    def test_recriar_compiles_the_exact_creation_message_and_replaces_the_routine_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, _compiled(_change()))
            self.seal(service, value)
            routine_incident.pause(service, "team_1", run_id, "decided")
            answered = self.answer(service, run_id, self.card(service, run_id), "recreate")
            state = self.state(service)
            source = routine_source.load(service, "team_1", value.routine_id)
        self.assertEqual(answered["status"], "recreated")
        ((payload, provider, model),) = brain.compiled
        # Exactly the sealed message and the current contracts, with the Team's own provider and the bound key.
        self.assertEqual(
            (payload["message"], payload["locale"], provider, model), (MESSAGE, None, "openai", "gpt-6-luna")
        )
        self.assertEqual(payload["provider"]["api_key"], CREDENTIAL[1])
        self.assertEqual([item["id"] for item in payload["assistants"]], [ASSISTANT])
        (routine,) = state.routines
        self.assertEqual(
            (routine.routine_id, routine.revision, routine.paused, routine.timezone),
            (value.routine_id, value.revision + 1, False, value.timezone),
        )
        self.assertEqual([step["id"] for step in routine.plan["steps"]], ["zones"])
        self.assertEqual(routine.plan["steps"][0]["input"]["per_page"], {"kind": "literal", "value": 25})
        self.assertGreaterEqual(routine.next_run_at, routine.anchor)
        self.assertEqual([item.status for item in state.incidents], ["released"])
        outcomes = {item.outcome: item for item in state.notices}
        self.assertEqual(outcomes["user-skipped"].detail["choice"], "recreate")
        self.assertIn("changed", outcomes)
        # The source is the creation message, never replaced by the recreated revision.
        self.assertEqual(source.parts, (("said", MESSAGE),))

    def test_recriar_recompiles_with_the_sealed_earlier_sends_and_admits_their_words(self) -> None:
        """A Routine whose message referred to earlier work recompiles from exactly that sealed source."""
        earlier = "list my zones, page 1 with 25 per page"
        cited = _change(request="Every day at 9, do this")
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, _compiled(cited), _compiled(cited))
            # Without its earlier send the same change cites words the message lacks, so it is refused.
            self.seal(service, value, message="Every day at 9, do this")
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
            service.routine_store.delete_source("team_1", value.routine_id)
            self.seal(service, value, message="Every day at 9, do this", earlier=(earlier,))
            self.assertEqual(
                self.answer(service, run_id, self.card(service, run_id), "recreate")["status"], "recreated"
            )
            (routine,) = self.state(service).routines
        self.assertEqual(
            [payload["draft"] for payload, _provider, _model in brain.compiled],
            [[], [{"kind": "cited", "text": earlier}]],
        )
        self.assertEqual(
            routine.grant["message"],
            routine_request.commitment((("cited", earlier), ("said", "Every day at 9, do this"))),
        )

    def test_a_question_is_answered_only_by_the_value_the_person_once_selected(self) -> None:
        asked = _change(schedule=None)
        asked["question"] = {
            "field": {"kind": "schedule"},
            "values": [{"kind": "daily", "time": "10:00"}, DAILY],
            "replies": ["Pronto: 10:00.", "Pronto: 09:00."],
        }
        clarification = {
            "question": "When?",
            # The recompile words its labels anew; the grant keeps the person's own sealed answer instead.
            "options": [{"label": "At ten", "description": ""}, {"label": "At nine", "description": ""}],
            "default_index": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held_with(
                directory, _compiled(asked, clarification), _compiled(asked, clarification)
            )
            self.seal(service, value)
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
            service.routine_store.delete_source("team_1", value.routine_id)
            self.seal_answer(service, value, "At 9", (("schedule",), DAILY))
            self.answer(service, run_id, self.card(service, run_id), "recreate")
            (routine,) = self.state(service).routines
        self.assertEqual(routine.schedule, DAILY)
        self.assertEqual(routine.grant["selected"], {"field": ["schedule"], "label": "At 9"})

    def test_a_recompiled_input_question_selects_the_sealed_member_value_or_refuses(self) -> None:
        """An input question is answered by the member value the person once selected; any other field refuses."""
        asked = _change()
        del asked["steps"][0]["input"]["per_page"]
        asked["question"] = {
            "field": {"kind": "input", "step": "zones", "member": "per_page"},
            "values": [50, 25],
            "replies": ["Pronto: 50.", "Pronto: 25."],
        }
        options = [{"label": "50 per page", "description": ""}, {"label": "25 per page", "description": ""}]
        clarification = {"question": "How many per page?", "options": options, "default_index": None}
        selected = (("input", "zones", "per_page"), {"kind": "literal", "value": 25})
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held_with(
                directory, _compiled(asked, clarification), _compiled(asked, clarification)
            )
            self.seal_answer(service, value, "25 per page", (("schedule",), DAILY))
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
            service.routine_store.delete_source("team_1", value.routine_id)
            self.seal_answer(service, value, "25 per page", selected)
            self.answer(service, run_id, self.card(service, run_id), "recreate")
            (routine,) = self.state(service).routines
        self.assertEqual(routine.plan["steps"][0]["input"]["per_page"], {"kind": "literal", "value": 25})
        self.assertEqual(routine.grant["selected"], {"field": list(selected[0]), "label": "25 per page"})

    def test_a_recompiled_zone_question_selects_the_sealed_zone(self) -> None:
        asked = _change()
        asked["question"] = {
            "field": {"kind": "timezone"},
            "values": ["UTC", "Europe/Lisbon"],
            "replies": ["Pronto: UTC.", "Pronto: Lisboa."],
        }
        options = [{"label": "UTC", "description": ""}, {"label": "Lisbon", "description": ""}]
        clarification = {"question": "Which zone?", "options": options, "default_index": None}
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held_with(directory, _compiled(asked, clarification))
            self.seal_answer(service, value, "Lisbon", (("timezone",), "Europe/Lisbon"))
            self.answer(service, run_id, self.card(service, run_id), "recreate")
            (routine,) = self.state(service).routines
        self.assertEqual((routine.timezone, routine.grant["selected"]["label"]), ("Europe/Lisbon", "Lisbon"))

    def test_a_recompiled_cap_question_selects_the_sealed_cap_in_any_option_order(self) -> None:
        """Only the option holding the sealed value is admitted, its cap proven by the person's own sealed label."""
        selected = {"kind": "continuous", "gap": 30, "cap": 500}
        for caps in ((100, 500), (500, 100)):
            asked = _change(schedule=None)
            asked["question"] = {
                "field": {"kind": "schedule"},
                "values": [{"kind": "continuous", "gap": 30, "cap": cap} for cap in caps],
                "replies": [f"Pronto: {cap}." for cap in caps],
            }
            options = [{"label": f"Up to {cap} a day", "description": ""} for cap in caps]
            clarification = {"question": "How many a day?", "options": options, "default_index": None}
            with self.subTest(caps=caps), tempfile.TemporaryDirectory() as directory:
                service, _brain, value, run_id = self.held_with(directory, _compiled(asked, clarification))
                self.seal_answer(service, value, "Até 500 por dia", (("schedule",), selected))
                self.answer(service, run_id, self.card(service, run_id), "recreate")
                (routine,) = self.state(service).routines
            self.assertEqual(routine.schedule, selected)
            self.assertEqual(routine.grant["selected"], {"field": ["schedule"], "label": "Até 500 por dia"})

    def test_recriar_changes_nothing_when_refused_unavailable_stopped_or_without_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(
                directory,
                _compiled(refusal="unsupported"),
                _compiled(refusal="unavailable"),
                brain_runtime_client.BrainRuntimeError("down"),
                _compiled(_change(steps=[{**_change()["steps"][0], "action": "no-such-action"}])),
                # A compile may only create; an update of any Routine, even a listed one, is refused.
                _compiled(_change(op="update", routine_id="f" * 32, expected_revision=1)),
            )
            self.refused(service, run_id, "recreate", "routine-source-unavailable")
            self.seal(service, value)
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
            self.refused(service, run_id, "recreate", "routine-recreate-unavailable")
            self.refused(service, run_id, "recreate", "routine-recreate-unavailable")
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
            # A person's Stop that lands while it compiles wins: nothing commits.
            brain.answers.append(_compiled(_change()))
            real = brain.routine_compile

            def stopped(payload, provider, model):
                service._stop_routine_run("team_1", run_id)
                return real(payload, provider, model)

            brain.routine_compile = stopped
            self.refused(service, run_id, "recreate", "routine-recovery-stopped")
            state = self.state(service)
        self.assertEqual(record.routine(state, value.routine_id).revision, value.revision)
        self.assertEqual(len(brain.compiled), 6)


class CorrectionTests(CardCase):
    """What the implementation audit required: no unknown workload, a kept selection, a held deletion, honest limits."""

    def test_an_attempt_whose_workload_is_unknown_or_unreadable_is_never_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            self.seal(service, value)
            cursor = routine_incident.open_recovery(service, "team_1", run_id).cursor
            for workload in ("", "assistant-container"):
                # An attempt Team never classified: no record of its workload, or Docker cannot say it stopped.
                service.routine_store.put_cursor("team_1", dataclasses.replace(cursor, fault="", workload=workload))
                with mock.patch.object(
                    service.assistant_lifecycle, "_assistant_container", side_effect=DockerException("down")
                ):
                    self.refused(service, run_id, "run", "routine-workload-unquiesced")
                    self.refused(service, run_id, "recreate", "routine-workload-unquiesced")

    def test_a_compile_that_settles_the_selected_field_otherwise_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(
                directory, _compiled(_change(schedule={"kind": "daily", "time": "10:00"})), _compiled(_change())
            )
            self.seal(service, value, selected=(("schedule",), DAILY))
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
            self.answer(service, run_id, self.card(service, run_id), "recreate")
            (routine,) = self.state(service).routines
        self.assertEqual((routine.schedule, routine.grant["selected"]), (DAILY, None))
        self.assertEqual(len(brain.compiled), 2)

    def held_with(self, directory: str, *answers: object):
        return RecriarTests.held_with(self, directory, *answers)

    def test_a_full_team_refuses_before_any_paid_compile_with_its_own_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, _compiled(_change()))
            self.seal(service, value)
            receipts = tuple((f"{index:064x}", int(time.time()) + 600) for index in range(record.MAX_RECEIPTS))
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, receipts=receipts), None))
            self.refused(service, run_id, "recreate", "routine-receipts-full")
        self.assertEqual(brain.compiled, [])
        self.assertEqual(
            (
                routine_incident._transition_problem("routine-rate-limit").code,
                routine_incident._transition_problem("x").code,
            ),
            ("routine-rate-limit", "routine-incident-unavailable"),
        )

    def test_deletion_waits_for_a_stopped_writer_before_releasing_what_its_run_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            service.assistant_lifecycle._fail_stop_action = mock.Mock()
            # A recovery of the held run is still unwinding when the Routine is deleted.
            routine_run.register_routine_run(service, "team_1", run_id, "unwinding", 60)
            deleting = service.delete_routine("team_1", value.routine_id)
            routine_watchdog.check(service)
            state = self.state(service)
            # Deletion is in progress: the set-aside incident and its cursor stay while the writer is registered.
            self.assertFalse(deleting["deleted"])
            self.assertEqual([item.status for item in state.incidents], ["skipped"])
            self.assertEqual(service.routine_store.cursors("team_1"), (run_id,))
            self.assertEqual([item.routine_id for item in state.routines], [value.routine_id])
            routine_run.unregister_routine_run(service, run_id)
            routine_watchdog.check(service)
            state = self.state(service)
            again = service.delete_routine("team_1", value.routine_id)
        self.assertEqual(([item.status for item in state.incidents], state.routines), (["released"], ()))
        self.assertEqual(again["deleted"], True)


class CardBookBoundTests(CardCase):
    """The book holds only cards someone could still answer: expired, settled, and deleted ones go."""

    @staticmethod
    def _card(incident: str, routine: str, expires_at: float) -> routine_card.Card:
        return routine_card.Card(PRINCIPAL, "a" * 64, incident, routine, 1, 1, "g", None, None, "n" * 32, expires_at)

    def test_expired_cards_are_swept_when_another_opens_or_is_answered(self) -> None:
        clock = [1000.0]
        book = routine_card.CardBook(now=lambda: clock[0])
        for index in range(1000):
            book.open("team_1", self._card(f"{index:032x}", "r" * 32, book.deadline()))
        clock[0] += routine_card.CARD_SECONDS
        fresh = self._card("f" * 32, "r" * 32, book.deadline())
        book.open("team_2", fresh)
        self.assertEqual(list(book._cards), [("team_2", fresh.incident_id)])
        clock[0] += routine_card.CARD_SECONDS
        self.assertIsNone(book.take("team_2", fresh.incident_id, fresh.nonce, PRINCIPAL))
        self.assertEqual(book._cards, {})

    def test_a_settled_incident_or_a_deleted_routine_takes_its_cards_with_it(self) -> None:
        book = routine_card.CardBook(now=lambda: 0.0)
        kept = self._card("k" * 32, "r" * 32, 10.0)
        other_team = self._card("o" * 32, "d" * 32, 10.0)
        for team_id, card in (
            ("team_1", self._card("s" * 32, "r" * 32, 10.0)),
            ("team_1", self._card("d" * 32, "d" * 32, 10.0)),
            ("team_1", kept),
            ("team_2", other_team),
        ):
            book.open(team_id, card)
        book.discard("team_1", "s" * 32)
        book.discard("team_1", "s" * 32)
        book.drop_routine("team_1", "d" * 32)
        self.assertEqual(set(book._cards), {("team_1", kept.incident_id), ("team_2", other_team.incident_id)})
        self.assertIs(book.take("team_1", kept.incident_id, kept.nonce, PRINCIPAL), kept)

    def test_deleting_the_routine_drops_its_open_card_and_releasing_the_incident_drops_it_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            service.assistant_lifecycle._fail_stop_action = mock.Mock()
            self.card(service, run_id)
            self.assertEqual(list(service.routine_cards._cards), [("team_1", run_id)])
            # A recovery still unwinding defers the release of the set-aside incident.
            routine_run.register_routine_run(service, "team_1", run_id, "unwinding", 60)
            service.delete_routine("team_1", value.routine_id)
            self.assertEqual(service.routine_cards._cards, {})
            # A card the book still held for the incident goes once the incident is released.
            service.routine_cards.open("team_1", self._card(run_id, "x" * 32, service.routine_cards.deadline()))
            routine_run.unregister_routine_run(service, run_id)
            routine_watchdog.check(service)
            state = self.state(service)
        self.assertEqual([item.status for item in state.incidents], ["released"])
        self.assertEqual(service.routine_cards._cards, {})


class AuditFollowUpTests(CardCase):
    def held_with(self, directory: str, *answers: object):
        return RecriarTests.held_with(self, directory, *answers)

    def test_a_selected_value_is_compared_as_exact_json_types_included(self) -> None:
        self.assertFalse(routine_recreate._same(1, True))
        self.assertFalse(routine_recreate._same({"value": [1]}, {"value": [True]}))
        self.assertFalse(routine_recreate._same(25, 25.0))
        self.assertTrue(routine_recreate._same({"b": 1, "a": [True]}, {"a": [True], "b": 1}))
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, _compiled(_change()))
            # The person once selected a value that equals the compiled 1 only under Python's loose equality.
            self.seal(service, value, selected=(("input", "zones", "page"), {"kind": "literal", "value": True}))
            self.refused(service, run_id, "recreate", "routine-recreate-refused")
        self.assertEqual(len(brain.compiled), 1)

    def test_recriar_preflights_every_notice_it_adds_before_paying_for_a_compile(self) -> None:
        room = record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINES

        def filler(count: int, routine_id: str) -> tuple[record.Notice, ...]:
            return tuple(
                record.Notice(f"{index:032x}", routine_id, "", "skipped", 0, {"missed": 1}, 1, "Every day at 9")
                for index in range(1, count + 1)
            )

        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held_with(directory, _compiled(_change()))
            self.seal(service, value)
            # The held run's notice was delivered: Recriar would add it again and the changed notice, two in all.
            service.routine_store.update(
                "team_1", lambda state: (dataclasses.replace(state, notices=filler(room - 1, value.routine_id)), None)
            )
            self.refused(service, run_id, "recreate", "notices-full")
            self.assertEqual(brain.compiled, [])
            # Still undelivered, the held notice is only replaced: one free slot is enough.
            held = record.Notice(
                run_id,
                value.routine_id,
                run_id,
                "held",
                0,
                {"assistant_id": ASSISTANT, "action": "create-record"},
                1,
                "Every day at 9",
            )
            service.routine_store.update(
                "team_1",
                lambda state: (dataclasses.replace(state, notices=(*filler(room - 2, value.routine_id), held)), None),
            )
            self.answer(service, run_id, self.card(service, run_id), "recreate")
        self.assertEqual(len(brain.compiled), 1)
