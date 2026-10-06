"""A Routine is recorded from the work a Local chat turn did and created only by confirming its card (ADR-0101)."""

from __future__ import annotations

import dataclasses
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from local_controller_harness import LocalContractCase

from inference import client as brain_runtime_client
from local import app as local_app
from local import audit as local_audit
from local.routine import contracts as routine_contracts
from local.routine import manage as routine_manage
from local.routine import store as routine_store
from protocol.http.v1 import progress as http_progress
from protocol.http.v1 import routine as http_routine
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record

PRINCIPAL = "a" * 32
OTHER = "b" * 32
ASSISTANT = "shimpz-cloudflare"
SHIMPZ = "023e105f4ecef8ad9ca31a8372d0c353"
ACCOUNT = {"id": "f" * 32, "name": "Owner"}
PAGE = {"page": 1, "per_page": 25, "count": 2, "total_count": 2, "total_pages": 1}
ZONES = {
    "zones": [
        {"id": "9a7806061c88ada191ed06f989cc3dac", "name": "example.com", "status": "active", "type": "full"},
        {"id": SHIMPZ, "name": "shimpz.com", "status": "active", "type": "full"},
    ],
    "pagination": PAGE,
}
for _zone in ZONES["zones"]:
    _zone.update(paused=False, account=ACCOUNT)
RECORDS = {
    "records": [
        {
            "id": "e" * 32,
            "type": "A",
            "name": "shimpz.com",
            "content": "192.0.2.1",
            "ttl": 300,
            "proxied": False,
            "proxiable": True,
        }
    ],
    "pagination": {**PAGE, "count": 1, "total_count": 1},
}
# The owner's four turns as Admin composes a clarification answer with the message and question before it.
MESSAGE = (
    "Cria uma rotina pra mim\n"
    "Pergunta: O que a rotina deve fazer?\nResposta: Listar registros DNS\n"
    "Pergunta: Com que frequência?\nResposta: A cada 30 segundos\n"
    "Pergunta: De qual zona?\nResposta: shimpz.com"
)
CONTINUOUS = {"kind": "continuous", "gap": 30, "cap": 1000}
HOURLY = {"kind": "hourly", "every": 1}


def _record(**changes: object) -> dict[str, object]:
    value = {
        "op": "record",
        "name": "DNS de shimpz.com",
        "schedule": CONTINUOUS,
        "timezone": None,
        "output": {"mode": "show", "when": None},
        "notes": "",
        "decide_actions": [],
        "replaces": None,
        "turn_date": time.strftime("%Y-%m-%d", time.gmtime()),
    }
    value.update(changes)
    return value


class Recording:
    """A scripted chat agent: it lists the zones, lists shimpz.com's records, then records the Routine."""

    def __init__(self, *outcomes: dict[str, object], calls: bool = True, between=lambda: None) -> None:
        self.outcomes = list(outcomes)
        self.calls = calls
        self.between = between
        self.contexts: list[brain_runtime_client.RuntimeContext] = []
        self.round = 0

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        self.round = 0
        if not self.calls:
            return self._done()
        zones = brain_runtime_client.ActionRequest("i-1", ASSISTANT, "list-zones", {"page": 1, "per_page": 25})
        return brain_runtime_client.RuntimeTurn("action-required", "", (zones,))

    def resume(self, _context, results):
        self.round += 1
        if self.round == 1:
            zone = next(item["id"] for item in results["i-1"]["zones"] if item["name"] == "shimpz.com")
            records = brain_runtime_client.ActionRequest(
                "i-2", ASSISTANT, "list-dns-records", {"zone_id": zone, "page": 1, "per_page": 25}
            )
            return brain_runtime_client.RuntimeTurn("action-required", "", (records,))
        self.between()
        return self._done()

    def _done(self):
        return brain_runtime_client.RuntimeTurn("completed", "Pronto.", (), routine=self.outcomes.pop(0))


def _body(message: str = MESSAGE, *, files=(), issued_at: int | None = None) -> dict[str, object]:
    return {
        "message": message,
        "files": list(files),
        "assistant_ids": [ASSISTANT],
        "conversation": [],
        "locale": "pt",
        "request": {"issued_at": int(time.time()) + 1 if issued_at is None else issued_at, "nonce": "c" * 32},
        "timezone": "America/Sao_Paulo",
    }


class RecordedRoutineTests(LocalContractCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(local_audit, "record_request", return_value="a" * 32)
        patch.start()
        self.addCleanup(patch.stop)

    def controller(self, directory: str, runtime) -> object:
        controller = self._chat_controller(directory, runtime)

        def invoke(_team, _assistant, action, _payload, _evidence):
            return {"result": ZONES if action == "list-zones" else RECORDS}

        controller.assistant_lifecycle.invoke = invoke
        return controller.chat_turn_service

    @staticmethod
    def as_person(principal: str = PRINCIPAL):
        return local_audit.bind_request_principal(local_audit.AuditPrincipal(principal, "human"))

    def chat(self, service, body: dict[str, object] | None = None) -> dict[str, object]:
        with self.as_person():
            return service.chat("team_1", body or _body(), "openai", "sk-test-0123456789")

    def confirm(self, service, proposal_id: str, principal: str = PRINCIPAL) -> dict[str, object]:
        with self.as_person(principal):
            return service.confirm_routine_proposal("team_1", proposal_id)

    def refusal(self, runtime, body: dict[str, object] | None = None, *, prepare=lambda service: None) -> str:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            prepare(service)
            response = self.chat(service, body)
            self.assertEqual(service.routine_store.load("team_1").routines, ())
        self.assertNotIn("routine_proposal", response)
        self.assertEqual(response["reply"], "Pronto.")
        return response["routine_refusal"]["code"]

    def test_the_owners_turn_records_a_card_whose_confirmation_creates_the_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            response = self.chat(service)
            card = response["routine_proposal"]
            self.assertEqual(service.routine_store.load("team_1").routines, ())
            answer = self.confirm(service, card["proposal_id"])
            again = self.confirm(service, card["proposal_id"])
            state = service.routine_store.load("team_1")
        self.assertEqual(http_routine.canonical_proposal(card), card)
        self.assertEqual((card["schedule"], card["clamped"], card["rehearsal"]), (CONTINUOUS, False, False))
        zones, records = card["steps"]
        self.assertEqual((zones["action"], records["action"]), ("list-zones", "list-dns-records"))
        zone_id = next(item for item in records["inputs"] if item["member"] == "zone_id")
        self.assertEqual(
            zone_id,
            {
                "member": "zone_id",
                "origin": "selector",
                "value": None,
                "step": 1,
                "pointer": "/zones",
                "where": {"member": "name", "value_json": '"shimpz.com"'},
                "item": "/id",
            },
        )
        (routine,) = state.routines
        self.assertEqual(answer, {**again, "status": "created"})
        self.assertEqual(answer["routine_id"], routine.routine_id)
        self.assertEqual(routine.confirmation["principal"], PRINCIPAL)
        self.assertEqual(routine.confirmation["proposal_id"], card["proposal_id"])
        self.assertEqual(routine.timezone, "America/Sao_Paulo")
        reference = routine.plan["steps"][1]["input"]["zone_id"]
        self.assertEqual(reference["where"], {"name": "shimpz.com"})
        (notice,) = state.notices
        self.assertEqual((notice.outcome, notice.name, notice.usage), ("created", "DNS de shimpz.com", None))

    def test_cancelling_revokes_the_card_and_it_never_confirms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service)["routine_proposal"]
            with self.as_person():
                answer = service.revoke_routine_proposal("team_1", card["proposal_id"])
                repeated = service.revoke_routine_proposal("team_1", card["proposal_id"])
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.confirm(service, card["proposal_id"])
        self.assertEqual(answer, repeated)
        self.assertEqual((answer["status"], answer["routine_id"]), ("revoked", None))
        self.assertEqual(caught.exception.code, "routine-proposal-expired")

    def test_a_card_belongs_to_its_person_and_lasts_fifteen_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record(), _record()))
            first = self.chat(service)["routine_proposal"]
            with self.assertRaises(local_app.ApiProblem) as other:
                self.confirm(service, first["proposal_id"], OTHER)
            # Another person's attempt consumed nothing of the owner's card, which a newer card then replaces.
            second = self.chat(service)["routine_proposal"]
            with self.assertRaises(local_app.ApiProblem) as replaced:
                self.confirm(service, first["proposal_id"])
            service.routine_proposals._now = lambda: time.monotonic() + 16 * 60
            with self.assertRaises(local_app.ApiProblem) as expired:
                self.confirm(service, second["proposal_id"])
            with self.assertRaises(local_app.ApiProblem) as nobody:
                service.confirm_routine_proposal("team_1", second["proposal_id"])
        self.assertEqual([item.exception.code for item in (other, replaced, expired)], ["routine-proposal-expired"] * 3)
        self.assertEqual(nobody.exception.code, "routine-card-person-required")

    def test_changed_assistants_or_another_team_incarnation_refuse_the_confirmation(self) -> None:
        cases = {
            "pins": lambda service: mock.patch.object(routine_contracts, "current_contracts", return_value={}),
            "unavailable": lambda service: mock.patch.object(
                routine_contracts, "current_contracts", side_effect=routine_contracts.ContractsUnavailableError
            ),
            "incarnation": lambda service: mock.patch.object(
                service.assistant_lifecycle, "_network", return_value=SimpleNamespace(id="c" * 64, name="team-network")
            ),
        }
        expected = {"pins": "team-context-changed", "unavailable": "team-context-unavailable"}
        for name, patch in cases.items():
            with tempfile.TemporaryDirectory() as directory, self.subTest(name=name):
                service = self.controller(directory, Recording(_record()))
                card = self.chat(service)["routine_proposal"]
                with patch(service), self.assertRaises(local_app.ApiProblem) as caught:
                    self.confirm(service, card["proposal_id"])
                self.assertEqual(caught.exception.code, expected.get(name, "team-context-changed"))
                self.assertEqual(service.routine_store.load("team_1").routines, ())

    def test_a_recording_that_cannot_become_a_routine_keeps_its_reply_and_carries_its_refusal(self) -> None:
        def mutating(service) -> None:
            spec = service.registry[ASSISTANT]
            action = dataclasses.replace(spec.actions["list-dns-records"], effect="mutating")
            service.registry[ASSISTANT] = dataclasses.replace(
                spec, actions={**spec.actions, "list-dns-records": action}
            )

        cases = [
            (Recording(_record(), calls=False), "routine-recording-empty", None),
            (Recording(_record(output={"mode": "decide", "when": "always"})), "routine-recording-invalid", None),
            (Recording(_record(notes="extra")), "routine-recording-invalid", None),
            (Recording(_record(replaces="d" * 32)), "routine-not-found", None),
            (Recording(_record()), "routine-mutation-unavailable", mutating),
        ]
        for runtime, code, prepare in cases:
            with self.subTest(code=code):
                self.assertEqual(self.refusal(runtime, prepare=prepare or (lambda service: None)), code)

    def test_a_recording_lost_mid_turn_is_unavailable(self) -> None:
        runtime = Recording(_record())

        def restart() -> None:
            # A Team restart leaves the resumed turn's recording naming nothing.
            runtime.service.routine_recordings.clear()

        runtime.between = restart

        def keep(service) -> None:
            runtime.service = service

        self.assertEqual(self.refusal(runtime, prepare=keep), "routine-recording-unavailable")

    def test_a_turn_with_files_or_a_stale_request_offers_no_routine_tool(self) -> None:
        for body in (_body(issued_at=int(time.time()) - 3600),):
            runtime = Recording(_record())
            with self.subTest(body=body):
                self.assertEqual(self.refusal(runtime, body), "routine-recording-unavailable")
                self.assertIsNone(runtime.contexts[0].routines)
                self.assertIsNone(runtime.contexts[0].routine_capacity)

    def test_a_card_too_large_for_its_bound_or_its_terminal_line_is_refused_whole(self) -> None:
        for target, name in ((http_routine, "MAX_PROPOSAL_BYTES"), (http_progress, "MAX_LINE_BYTES")):
            with self.subTest(name=name), mock.patch.object(target, name, 512):
                self.assertEqual(self.refusal(Recording(_record())), "routine-proposal-too-large")

    def test_a_continuous_cap_is_clamped_to_what_the_team_has_left_and_refused_when_nothing_is(self) -> None:
        with (
            mock.patch.object(routine_definition, "capacity", return_value=2 * 400),
            tempfile.TemporaryDirectory() as directory,
        ):
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service)["routine_proposal"]
        self.assertEqual((card["schedule"]["cap"], card["clamped"]), (400, True))
        with mock.patch.object(routine_definition, "capacity", return_value=1):
            self.assertEqual(self.refusal(Recording(_record())), "routine-step-budget")

    def test_a_replacement_without_actions_keeps_the_steps_and_confirms_as_the_next_revision(self) -> None:
        hourly = {"kind": "hourly", "every": 1}
        with tempfile.TemporaryDirectory() as directory:
            runtime = Recording(_record())
            service = self.controller(directory, runtime)
            created = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])
            routine_id = created["routine_id"]
            runtime.calls = False
            runtime.outcomes.append(_record(schedule=hourly, replaces=routine_id, name="DNS por hora"))
            card = self.chat(service)["routine_proposal"]
            answer = self.confirm(service, card["proposal_id"])
            state = service.routine_store.load("team_1")
        (routine,) = state.routines
        self.assertEqual((card["replaces"], answer["status"], routine.revision), (routine_id, "changed", 2))
        self.assertEqual((routine.schedule, routine.name, len(routine.plan["steps"])), (hourly, "DNS por hora", 2))
        self.assertEqual([item.name for item in state.notices], ["DNS de shimpz.com", "DNS por hora"])

    def test_a_replaced_routine_that_changed_or_went_refuses_the_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Recording(_record())
            service = self.controller(directory, runtime)
            routine_id = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])["routine_id"]
            runtime.calls = False
            runtime.outcomes.extend([_record(replaces=routine_id), _record(replaces=routine_id)])
            stale = self.chat(service)["routine_proposal"]
            service.routine_store.update(
                "team_1",
                lambda state: (
                    dataclasses.replace(
                        state,
                        routines=tuple(
                            dataclasses.replace(item, revision=item.revision + 1) for item in state.routines
                        ),
                    ),
                    None,
                ),
            )
            with self.assertRaises(local_app.ApiProblem) as changed:
                self.confirm(service, stale["proposal_id"])
            gone = self.chat(service)["routine_proposal"]
            with self.as_person():
                routine_manage.delete_routine(service, "team_1", routine_id)
            with self.assertRaises(local_app.ApiProblem) as deleted:
                self.confirm(service, gone["proposal_id"])
            notices = service.routine_store.load("team_1").notices
        self.assertEqual(changed.exception.code, "routine-revision-changed")
        self.assertEqual(deleted.exception.code, "routine-proposal-expired")
        self.assertEqual(notices[-1].outcome, "deleted")
        self.assertEqual((notices[-1].name, notices[-1].run_id, notices[-1].usage), ("DNS de shimpz.com", "", None))

    def test_a_replacement_binds_the_revision_the_turn_was_shown(self) -> None:
        def bump(service) -> None:
            service.routine_store.update(
                "team_1",
                lambda state: (
                    dataclasses.replace(
                        state,
                        routines=tuple(
                            dataclasses.replace(item, revision=item.revision + 1) for item in state.routines
                        ),
                    ),
                    None,
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            runtime = Recording(_record(schedule=HOURLY))
            service = self.controller(directory, runtime)
            routine_id = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])["routine_id"]
            # The Routine moves on while the turn that was shown its first revision is still running.
            runtime.between = lambda: bump(service)
            runtime.outcomes.append(_record(schedule=HOURLY, replaces=routine_id))
            changed = self.chat(service)
            runtime.between = lambda: None
            runtime.outcomes.append(_record(schedule=HOURLY))
            created = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])["routine_id"]
            # A Routine created after a turn's listing was never shown to it, so the turn cannot replace it.
            service.routine_recordings.listed = lambda *_args: None
            runtime.outcomes.append(_record(schedule=HOURLY, replaces=created))
            unlisted = self.chat(service)
        self.assertEqual(changed["routine_refusal"]["code"], "routine-revision-changed")
        self.assertEqual(unlisted["routine_refusal"]["code"], "routine-not-found")

    def test_a_stop_that_wins_leaves_no_card(self) -> None:
        runtime = Recording(_record())

        def stop() -> None:
            runtime.service.stop_chat("team_1")

        runtime.between = stop
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            runtime.service = service
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.chat(service)
            cards = dict(service.routine_proposals._cards)
        self.assertEqual((caught.exception.code, cards), ("chat-stopped", {}))

    def test_an_ambiguous_commit_fails_closed_and_the_card_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service)["routine_proposal"]
            with (
                mock.patch.object(service.routine_store, "update", side_effect=routine_store.RoutineStoreError),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                self.confirm(service, card["proposal_id"])
            with self.assertRaises(local_app.ApiProblem) as again:
                self.confirm(service, card["proposal_id"])
        self.assertEqual(
            (caught.exception.code, again.exception.code), ("routine-state-unavailable", "routine-proposal-expired")
        )

    def test_a_full_team_refuses_the_confirmed_definition_in_its_own_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service)["routine_proposal"]
            with (
                mock.patch.object(record, "add_routine", side_effect=record.RoutineStateError("routine-limit")),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                self.confirm(service, card["proposal_id"])
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "routine-limit"))

    def test_each_later_refusal_keeps_the_reply_and_creates_nothing(self) -> None:
        cases = [
            ("routine-recording-invalid", {"outcome": _record(timezone="Mars/Olympus")}),
            ("plan-input-type", {"patch": (routine_plan, "admit", routine_plan.PlanError("plan-input-type"))}),
            ("routine-invalid", {"patch": (record, "scheduled", record.RoutineStateError("routine-invalid"))}),
            ("routine-rate-limit", {"patch": (routine_definition, "over_budget", None), "value": "routine-rate-limit"}),
            ("routine-secret-literal", {"outcome": _record(name="DNS tok-protected-1"), "protect": "tok-protected-1"}),
        ]
        for code, case in cases:
            runtime = Recording(case.get("outcome", _record()))
            protect = case.get("protect")

            def prepare(service, protect=protect, runtime=runtime) -> None:
                if protect:
                    original = service.routine_recordings.start

                    def start(*args, **options):
                        recording_id = original(*args, **options)
                        service.routine_recordings.protect(args[0], recording_id, (protect,))
                        return recording_id

                    service.routine_recordings.start = start

            patch = case.get("patch")
            with self.subTest(code=code):
                if patch is None:
                    self.assertEqual(self.refusal(runtime, prepare=prepare), code)
                    continue
                target, name, error = patch
                kwargs = {"return_value": case["value"]} if "value" in case else {"side_effect": error}
                with mock.patch.object(target, name, **kwargs):
                    self.assertEqual(self.refusal(runtime, prepare=prepare), code)

    def test_a_replaced_routine_with_a_live_run_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Recording(_record())
            service = self.controller(directory, runtime)
            routine_id = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])["routine_id"]
            live = record.Run(
                "f" * 32,
                routine_id,
                "frozen",
                1,
                request_kind="human",
                assistant_id=ASSISTANT,
                action="list-zones",
                position={"phase": "replay", "step": 1},
                steps=2,
            )
            service.routine_store.update(
                "team_1", lambda state: (record.dataclasses.replace(state, runs=(live,)), None)
            )
            runtime.calls = False
            runtime.outcomes.append(_record(replaces=routine_id))
            busy = self.chat(service)["routine_refusal"]["code"]
        self.assertEqual(busy, "routine-busy")

    def test_a_card_its_own_contract_refuses_is_an_internal_error_never_shown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            with (
                mock.patch.object(http_routine, "canonical_proposal", return_value=None),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                self.chat(service)
            self.assertEqual(dict(service.routine_proposals._cards), {})
        self.assertEqual(caught.exception.code, "internal-error")


if __name__ == "__main__":
    unittest.main()
