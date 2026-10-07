"""A Routine is recorded from the work a Local chat turn did and created only by confirming its card (ADR-0101)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import routine_fixture
from local_controller_harness import LocalContractCase

from action import human as action_human
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import app as local_app
from local import audit as local_audit
from local import authority as local_authority
from local.chat import api as local_chat_api
from local.chat import segment as local_segment
from local.routine import contracts as routine_contracts
from local.routine import manage as routine_manage
from local.routine import proposal as routine_proposal
from local.routine import recorder as local_routine_recorder
from local.routine import store as routine_store
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import progress as http_progress
from protocol.http.v1 import routine_context as http_routine_context
from protocol.http.v1 import routine_proposal as http_routine_proposal
from routine import compose as routine_compose
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record
from routine import recording as routine_recording
from tests import human_request_fixtures

PRINCIPAL = "a" * 32
# The Routine key fingerprint Team admits for a run in these tests.
KEY = "e" * 64
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
    "Pergunta: De qual zona?\nResposta: shimpz.com\n"
    "Pergunta: O que fazer com o resultado?\nResposta: Mostrar em todas as execuções"
)
CONTINUOUS = {"kind": "continuous", "gap": 30, "cap": 2880}


def _record(**changes: object) -> dict[str, object]:
    value = {
        "op": "record",
        "name": "DNS de shimpz.com",
        "notes": "",
        "decide_actions": [],
        "replaces": None,
        "turn_date": time.strftime("%Y-%m-%d", time.gmtime()),
    }
    value.update(changes)
    return value


def _bump_revisions(service) -> None:
    """Move every Routine of team_1 to its next revision, as a change made meanwhile would."""
    service.routine_store.update(
        "team_1",
        lambda state: (
            dataclasses.replace(
                state, routines=tuple(dataclasses.replace(item, revision=item.revision + 1) for item in state.routines)
            ),
            None,
        ),
    )


class Recording:
    """A scripted chat agent: it lists the zones, lists shimpz.com's records, then records the Routine."""

    # A paused Action's request carries no model-written purpose.
    purpose = staticmethod(lambda *_args: None)

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


class Sends:
    """A scripted chat agent across sends: each send runs its own calls, then replies, recording only when told to."""

    def __init__(self, *scripts: tuple[tuple[str, ...], dict[str, object] | None]) -> None:
        self.scripts = list(scripts)
        self.contexts: list[brain_runtime_client.RuntimeContext] = []

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        self.calls, self.outcome = self.scripts.pop(0)
        self.done = 0
        return self._next()

    def resume(self, _context, _results):
        return self._next()

    def _next(self):
        if self.done == len(self.calls):
            return brain_runtime_client.RuntimeTurn("completed", "Pronto.", (), routine=self.outcome)
        action = self.calls[self.done]
        self.done += 1
        given = {"page": 1, "per_page": 25}
        if action == "list-dns-records":
            # The agent remembers shimpz.com's id from a lookup, whenever it ran.
            given = {"zone_id": SHIMPZ, **given}
        request = brain_runtime_client.ActionRequest(f"i-{self.done}", ASSISTANT, action, given)
        return brain_runtime_client.RuntimeTurn("action-required", "", (request,))


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

    def turn(self, runtime, body: dict[str, object] | None, prepare) -> tuple[tuple, dict[str, object]]:
        """One person's turn on a fresh Team: the Routines it then holds, and the reply."""
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            prepare(service)
            response = self.chat(service, body)
            return service.routine_store.load("team_1").routines, response

    def refusal(self, runtime, body: dict[str, object] | None = None, *, prepare=lambda service: None) -> str:
        routines, response = self.turn(runtime, body, prepare)
        self.assertEqual(routines, ())
        self.assertNotIn("routine_proposal", response)
        self.assertEqual(response["reply"], "Pronto.")
        return response["routine_refusal"]["code"]

    def question(self, runtime, body: dict[str, object] | None = None, *, prepare=lambda service: None) -> dict:
        routines, response = self.turn(runtime, body, prepare)
        self.assertEqual(routines, ())
        self.assertNotIn("routine_proposal", response)
        self.assertEqual(response["reply"], "Pronto.")
        return response["routine_question"]

    def test_the_owners_turn_records_a_card_whose_confirmation_creates_the_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            response = self.chat(service)
            card = response["routine_proposal"]
            self.assertEqual(service.routine_store.load("team_1").routines, ())
            answer = self.confirm(service, card["proposal_id"])
            again = self.confirm(service, card["proposal_id"])
            state = service.routine_store.load("team_1")
        self.assertEqual(http_routine_proposal.canonical_proposal(card), card)
        self.assertEqual((card["schedule"], card["timezone_source"]), (CONTINUOUS, "browser"))
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

    def test_a_zone_the_person_named_in_an_earlier_send_selects_the_zone_id(self) -> None:
        body = _body("Faça isso a cada 30 segundos e mostre o resultado")
        body["conversation"] = [
            {"role": "user", "text": "Liste os registros DNS de shimpz.com", "truncated": False},
            {"role": "assistant", "text": "Listei os registros.", "truncated": False},
        ]
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service, body)["routine_proposal"]
        zone_id = next(item for item in card["steps"][1]["inputs"] if item["member"] == "zone_id")
        self.assertEqual((zone_id["origin"], zone_id["where"]["value_json"]), ("selector", '"shimpz.com"'))
        # Without the earlier send, nothing the person wrote names the zone, so the person is asked which it is.
        asked = self.question(Recording(_record()), _body("Faça isso a cada 30 segundos e mostre o resultado"))
        self.assertEqual(
            (asked["code"], [item["label"] for item in asked["options"]]),
            ("routine-binding-ambiguous", ["example.com", "shimpz.com"]),
        )

    def test_a_lookup_in_an_earlier_send_is_the_source_of_the_remembered_id(self) -> None:
        runtime = Sends((("list-zones",), None), (("list-dns-records",), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            first = self.chat(service, _body("Liste os registros DNS"))
            card = self.chat(service, _body("shimpz.com, a cada 30 segundos, mostrar sempre"))["routine_proposal"]
        self.assertNotIn("routine_question", first)
        self.assertEqual([step["action"] for step in card["steps"]], ["list-zones", "list-dns-records"])
        zone_id = next(item for item in card["steps"][1]["inputs"] if item["member"] == "zone_id")
        self.assertEqual((zone_id["origin"], zone_id["where"]["value_json"]), ("selector", '"shimpz.com"'))

    def test_a_question_keeps_the_span_and_the_persons_answer_completes_the_card(self) -> None:
        runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body("Liste os registros DNS de shimpz.com e mostre o resultado"))[
                "routine_question"
            ]
            card = self.chat(service, _body("A cada 30 segundos"))["routine_proposal"]
            span = service.routine_recordings._spans.get("team_1")
        self.assertEqual(asked, {"code": "routine-schedule-unstated", "options": [], "value": None})
        self.assertEqual(
            (card["schedule"], [step["action"] for step in card["steps"]]),
            (CONTINUOUS, ["list-zones", "list-dns-records"]),
        )
        # The card ended the span.
        self.assertIsNone(span)

    def test_a_span_consumed_after_its_fifteen_minutes_is_unavailable(self) -> None:
        def late(seconds: int):
            def prepare(service) -> None:
                started = time.time()
                service.routine_recordings._clock = lambda: started + seconds

            return prepare

        self.assertEqual(self.refusal(Recording(_record()), prepare=late(901)), "routine-recording-unavailable")
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            late(899)(service)
            self.assertIn("routine_proposal", self.chat(service))

    def test_a_question_holding_a_protected_value_is_refused_never_published(self) -> None:
        leaking = routine_recording.Question(
            "routine-binding-ambiguous", ({"value": "tok-protected-9", "label": None},)
        )
        runtime = Recording(_record())

        def prepare(service) -> None:
            original = service.routine_recordings.start

            def start(*args, **options):
                recording_id = original(*args, **options)
                service.routine_recordings.protect(args[0], recording_id, ("tok-protected-9",))
                return recording_id

            service.routine_recordings.start = start

        with mock.patch.object(routine_compose, "record", return_value=leaking):
            self.assertEqual(self.refusal(runtime, prepare=prepare), "routine-secret-literal")

    def test_a_question_its_own_contract_refuses_is_an_internal_error_never_asked(self) -> None:
        stray = routine_recording.Question("routine-other")
        with (
            mock.patch.object(routine_compose, "record", return_value=stray),
            self.assertRaises(local_app.ApiProblem) as caught,
            tempfile.TemporaryDirectory() as directory,
        ):
            self.chat(self.controller(directory, Recording(_record())))
        self.assertEqual(caught.exception.code, "internal-error")

    def test_a_send_with_files_ends_the_span(self) -> None:
        runtime = Sends((("list-zones",), None), (("list-dns-records",), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            self.chat(service, _body("Liste os registros DNS de shimpz.com"))
            recording = local_chat_api._recording(
                service, "team_1", {"issued_at": int(time.time())}, ("m", ["f"], None, ())
            )
            asked = self.chat(service, _body("shimpz.com, a cada 30 segundos, mostrar sempre"))["routine_question"]
        self.assertIsNone(recording)
        self.assertEqual(asked["code"], "routine-binding-unsourced")

    def test_the_brain_sees_where_each_routines_timezone_came_from(self) -> None:
        runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), None))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])
            self.chat(service, _body("Quais rotinas eu tenho?"))
        ((listed),) = runtime.contexts[1].routines
        self.assertEqual((listed["timezone"], listed["timezone_source"]), ("America/Sao_Paulo", "browser"))
        self.assertEqual(http_routine_context.canonical_routine_listings(runtime.contexts[1].routines), [listed])

    def test_a_routine_listing_outside_the_brain_form_is_an_internal_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])
            with (
                mock.patch.object(http_routine_context, "canonical_routine_listings", return_value=None),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                routine_proposal.chat_routines(service, "team_1")
        self.assertEqual(caught.exception.code, "internal-error")

    def test_a_replacement_runs_in_the_requests_zone_not_the_replaced_routines(self) -> None:
        runtime = Recording(_record())
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            london = self.chat(service, _body(f"{MESSAGE}\nNo fuso Europe/London"))["routine_proposal"]
            routine_id = self.confirm(service, london["proposal_id"])["routine_id"]
            runtime.calls = False
            runtime.outcomes.append(_record(replaces=routine_id))
            card = self.chat(service, {**_body("Mantenha como está"), "timezone": "Asia/Tokyo"})["routine_proposal"]
        self.assertEqual(london["timezone"], "Europe/London")
        self.assertEqual((card["timezone"], card["timezone_source"]), ("Asia/Tokyo", "browser"))

    def test_a_composed_answer_to_the_pending_question_records_without_the_brain(self) -> None:
        original = "Liste os registros DNS de shimpz.com e mostre o resultado"
        runtime = Sends((("list-zones", "list-dns-records"), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body(original))["routine_question"]
            answer = http_payload.compose_clarified(original, "Com que frequência?", "A cada 30 segundos", "pt")
            response = self.chat(service, _body(answer))
        self.assertEqual(asked["code"], "routine-schedule-unstated")
        self.assertEqual(len(runtime.contexts), 1)
        self.assertEqual(response["reply"], http_routine_proposal.ANSWER_REPLIES["pt"])
        self.assertEqual(response["routine_proposal"]["schedule"], CONTINUOUS)

    def test_the_output_is_the_persons_to_state_and_a_composed_answer_states_it_without_the_brain(self) -> None:
        original = "Liste os registros DNS de shimpz.com a cada 30 segundos"
        runtime = Sends((("list-zones", "list-dns-records"), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body(original))["routine_question"]
            choice = http_routine_proposal.OUTPUT_CHOICES["pt"]["changes"]
            answer = http_payload.compose_clarified(original, "O que fazer com o resultado?", choice, "pt")
            card = self.chat(service, _body(answer))["routine_proposal"]
        self.assertEqual(asked, {"code": "routine-output-unstated", "options": [], "value": None})
        self.assertEqual((len(runtime.contexts), card["output"]), (1, {"mode": "changes", "when": None}))

    def test_a_chain_answer_reaches_the_brain_which_runs_the_chained_work(self) -> None:
        original = "Liste as zonas de shimpz.com a cada 30 segundos"
        runtime = Sends((("list-zones",), _record()), (("list-zones", "list-dns-records"), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body(original))["routine_question"]
            choice = http_routine_proposal.OUTPUT_CHOICES["pt"]["chain"]
            answer = http_payload.compose_clarified(original, "O que fazer com o resultado?", choice, "pt")
            card = self.chat(service, _body(answer))["routine_proposal"]
        self.assertEqual(asked["code"], "routine-output-unstated")
        self.assertEqual(len(runtime.contexts), 2)
        self.assertEqual([step["action"] for step in card["steps"]], ["list-zones", "list-dns-records"])
        self.assertEqual(card["output"], {"mode": "show", "when": None})

    def test_a_freely_typed_send_reaches_the_brain_with_the_pending_question(self) -> None:
        original = "Liste os registros DNS de shimpz.com"
        runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), None))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            self.chat(service, _body(original))
            response = self.chat(service, _body("A cada 30 segundos"))
        self.assertEqual((len(runtime.contexts), response["reply"]), (2, "Pronto."))
        self.assertNotIn("routine_proposal", response)
        question = {"code": "routine-schedule-unstated", "options": [], "value": None}
        self.assertEqual((runtime.contexts[0].routine_question, runtime.contexts[1].routine_question), (None, question))
        self.assertEqual([context.routine_mode for context in runtime.contexts], [False, True])

    def test_a_routine_mode_turn_on_openai_reasons_with_the_routine_model(self) -> None:
        runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), None))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            self.chat(service, _body("Liste os registros DNS de shimpz.com"))
            self.chat(service, _body("A cada 30 segundos"))
        self.assertEqual(
            [(context.routine_mode, context.model, context.effort) for context in runtime.contexts],
            [(False, "gpt-6-luna", "low"), (True, local_segment.ROUTINE_OPENAI_MODEL, "low")],
        )
        self.assertEqual({context.api_key for context in runtime.contexts}, {"sk-test-0123456789"})

    def test_the_routine_model_is_one_openai_offers(self) -> None:
        self.assertEqual(local_segment.ROUTINE_OPENAI_MODEL, "gpt-6.1-sol")
        self.assertIn(local_segment.ROUTINE_OPENAI_MODEL, inference_config.PROVIDERS["openai"]["models"])

    def test_a_routine_mode_turn_on_anthropic_keeps_the_team_model(self) -> None:
        runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), None))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            service.inference_store.save("team_1", inference_config.normalize("anthropic", "claude-sonnet-5-5"))
            with self.as_person():
                for message in ("Liste os registros DNS de shimpz.com", "A cada 30 segundos"):
                    service.chat("team_1", _body(message), "anthropic", "sk-ant-test-0123456789")
        self.assertEqual(
            [(context.routine_mode, context.model) for context in runtime.contexts],
            [(False, "claude-sonnet-5-5"), (True, "claude-sonnet-5-5")],
        )

    def test_a_turn_outside_routine_mode_keeps_the_team_model(self) -> None:
        runtime = Sends((("list-zones",), None), (("list-zones",), None))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            for message in ("Liste as zonas de shimpz.com", "E agora liste de novo"):
                self.chat(service, _body(message))
        self.assertEqual(
            [(context.routine_mode, context.model) for context in runtime.contexts],
            [(False, "gpt-6-luna"), (False, "gpt-6-luna")],
        )

    def test_a_start_reads_its_span_and_a_resume_keeps_the_model_its_start_pinned(self) -> None:
        book = local_routine_recorder.RecordingBook()
        started = local_routine_recorder.Started("A cada 30 segundos", (), None)
        recording = book.start("team_1", (PRINCIPAL, "i"), started, int(time.time()))
        controller = SimpleNamespace(routine_recordings=book)
        config = inference_config.normalize("openai", "gpt-6-luna")
        sol = local_segment.ROUTINE_OPENAI_MODEL
        cases = (
            (None, None, sol),
            (None, object(), "gpt-6-luna"),
            (sol, None, sol),
            ("gpt-6-luna", None, "gpt-6-luna"),
        )
        for model, routine, expected in cases:
            request = SimpleNamespace(team_id="team_1", recording=recording, model=model, routine=routine)
            with self.subTest(model=model, routine=routine):
                self.assertEqual(local_segment._turn_model(controller, request, config), expected)
        book.drop("team_1")
        for model, expected in ((None, "gpt-6-luna"), (sol, sol)):
            request = SimpleNamespace(team_id="team_1", recording=recording, model=model, routine=None)
            with self.subTest(model=model, span=None):
                self.assertEqual(local_segment._turn_model(controller, request, config), expected)

    def test_only_a_target_chosen_by_its_exact_json_text_skips_the_brain(self) -> None:
        original = "Liste os registros DNS a cada 30 segundos e mostre o resultado"
        for answer, brain in ((json.dumps(SHIMPZ), False), ("shimpz.com", True)):
            runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), None))
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                service = self.controller(directory, runtime)
                asked = self.chat(service, _body(original))["routine_question"]
                self.assertEqual(asked["code"], "routine-binding-ambiguous")
                composed = http_payload.compose_clarified(original, "Qual zona?", answer, "pt")
                response = self.chat(service, _body(composed))
                self.assertEqual(len(runtime.contexts), 1 + brain)
                self.assertEqual("routine_proposal" in response, not brain)

    def test_an_answer_replaces_only_the_revision_the_record_call_was_shown(self) -> None:
        def remove(service) -> None:
            with self.as_person():
                routine_manage.delete_routine(service, "team_1", service.created)

        # Naming no zone, the replacement asks which one; a replacement keeps its own schedule.
        original = "Liste os registros DNS"
        for change, code in ((_bump_revisions, "routine-revision-changed"), (remove, "routine-not-found")):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                runtime = Sends((("list-zones", "list-dns-records"), _record()))
                service = self.controller(directory, runtime)
                service.created = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])[
                    "routine_id"
                ]
                runtime.scripts.append((("list-zones", "list-dns-records"), _record(replaces=service.created)))
                self.chat(service, _body(original))
                change(service)
                answer = http_payload.compose_clarified(original, "Qual zona?", json.dumps(SHIMPZ), "pt")
                self.assertEqual(self.chat(service, _body(answer))["routine_refusal"]["code"], code)

    def test_a_record_call_for_new_work_supersedes_the_pending_question(self) -> None:
        runtime = Sends(
            (("list-dns-records",), _record()),
            (("list-zones",), _record(name="Zonas")),
            (("list-dns-records",), _record()),
            ((), _record()),
        )
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(
                service, _body("Liste os registros DNS de shimpz.com a cada 30 segundos e mostre o resultado")
            )
            zones = self.chat(service, _body("Agora só liste as zonas a cada 30 segundos e mostre o resultado"))
            again = self.chat(
                service, _body("Liste os registros DNS de shimpz.com a cada 30 segundos e mostre o resultado")
            )
            continued = self.chat(service, _body("Pode continuar"))
        self.assertEqual(asked["routine_question"]["code"], "routine-binding-unsourced")
        self.assertEqual(
            (zones["routine_proposal"]["name"], [step["action"] for step in zones["routine_proposal"]["steps"]]),
            ("Zonas", ["list-zones"]),
        )
        # The same intent recorded again keeps waiting for the verified rerun.
        self.assertEqual(again["routine_question"]["code"], "routine-binding-unsourced")
        self.assertEqual(continued["routine_question"]["code"], "routine-binding-unsourced")

    def test_new_work_drops_the_chain_anchor_of_the_question_it_supersedes(self) -> None:
        runtime = Sends(
            (("list-zones", "list-dns-records"), _record(name="DNS")),
            (("list-zones", "list-dns-records"), _record(name="Zonas e DNS")),
        )
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body("DNS de shimpz.com a cada 30 segundos"))["routine_question"]
            card = self.chat(service, _body("Liste as zonas de shimpz.com a cada 30 segundos e use em outras ações"))
        self.assertEqual(asked["code"], "routine-output-unstated")
        self.assertEqual(card["routine_proposal"]["name"], "Zonas e DNS")

    def test_a_verified_rerun_that_ends_in_prose_applies_the_stored_intent(self) -> None:
        runtime = Sends(
            (("list-dns-records",), _record()),
            (("list-zones",), None),
            (("list-zones", "list-dns-records"), None),
        )
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(
                service, _body("Liste os registros DNS de shimpz.com a cada 30 segundos e mostre o resultado")
            )
            partial = self.chat(service, _body("Pode buscar a zona"))
            rerun = self.chat(service, _body("Pode buscar de novo"))
        self.assertEqual(asked["routine_question"]["code"], "routine-binding-unsourced")
        self.assertEqual(
            (partial["reply"], "routine_proposal" in partial, "routine_question" in partial), ("Pronto.", False, False)
        )
        # The Brain sees the work to run again: the remembered zone needs a fresh source.
        (slot,) = runtime.contexts[1].routine_rerun
        kinds = {item["member"]: item["kind"] for item in slot["inputs"]}
        self.assertEqual(
            (slot["action"], kinds), ("list-dns-records", {"page": "value", "per_page": "value", "zone_id": "fresh"})
        )
        self.assertIsNone(runtime.contexts[0].routine_rerun)
        self.assertEqual(rerun["reply"], "Pronto.")
        zone_id = next(item for item in rerun["routine_proposal"]["steps"][1]["inputs"] if item["member"] == "zone_id")
        self.assertEqual(zone_id["where"]["value_json"], '"shimpz.com"')

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
        cases = [
            (Recording(_record(), calls=False), "routine-recording-empty", None),
            (Recording(_record(notes="extra")), "routine-recording-invalid", None),
            (
                Recording(_record(decide_actions=[{"assistant": ASSISTANT, "action": "list-zones"}])),
                "routine-recording-invalid",
                None,
            ),
            (Recording(_record(replaces="d" * 32)), "routine-not-found", None),
        ]
        for runtime, code, prepare in cases:
            with self.subTest(code=code):
                self.assertEqual(self.refusal(runtime, prepare=prepare or (lambda service: None)), code)

    def test_a_changing_recording_becomes_a_routine_whose_every_run_waits_for_its_own_authorization(self) -> None:
        """A change the person authorized while recording replays, and each run asks for that authorization again."""
        password = human_request_fixtures.admit(
            human_request_fixtures.fingerprinted(
                {"kind": "auth:password", "ordinal": 0, "title": "Sign in", "description": "Enter the password."}
            ),
            ("auth:password",),
        )
        changed: list[dict[str, object]] = []

        def invoke(_team, _assistant, action, payload, evidence):
            if action == "list-dns-records":
                if not any(item.kind == "auth:password" for item in evidence.transcript.responses):
                    raise action_human.HumanRequestSuspensionError(password)
                changed.append(dict(payload))
            return {"result": ZONES if action == "list-zones" else RECORDS}

        def run(service) -> dict[str, object]:
            now = int(time.time())
            service.routine_store.update(
                "team_1",
                lambda state: (
                    dataclasses.replace(
                        state, routines=tuple(dataclasses.replace(item, run_requested=now) for item in state.routines)
                    ),
                    None,
                ),
            )
            claim = service.claim_routine_run()
            evidence = local_authority.RoutineEvidence(KEY, record.lease_sha256(claim["lease_token"]), "a" * 32, 0)
            claimed = (claim["revision"], claim["plan_digest"], claim["mode"])
            return claim["run_id"], service.run_routine("team_1", claim["run_id"], evidence, claimed, ("openai", ""))

        def answer(service, run_id: str, decision: str) -> dict[str, object]:
            opened = service.open_routine_challenge("team_1", run_id, "pt")
            body = {"challenge_id": opened["challenge_id"], "decision": decision}
            body.update({"value": True} if decision == "submit" else {})
            return service.resume_routine_human("team_1", run_id, body, "openai", "")

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(local_authority, "routine_key_fingerprint", return_value=KEY),
            mock.patch.object(local_audit, "record", return_value="a" * 32),
        ):
            service = self.controller(directory, Recording(_record()))
            spec = service.registry[ASSISTANT]
            changing = dataclasses.replace(
                spec.actions["list-dns-records"], effect="mutating", human_requests=("auth:password",)
            )
            service.registry[ASSISTANT] = dataclasses.replace(
                spec, actions={**spec.actions, "list-dns-records": changing}
            )
            service.assistant_lifecycle.invoke = invoke
            paused = self.chat(service)
            with self.as_person():
                body = {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True}
                card = service.resume_chat_human("team_1", body, "openai", "sk-test-0123456789")["routine_proposal"]
            created = self.confirm(service, card["proposal_id"])
            recorded = len(changed)
            first, frozen = run(service)
            waiting = len(changed)
            approved = answer(service, first, "submit")
            second, again = run(service)
            denied = answer(service, second, "deny")
        self.assertEqual((paused["status"], paused["request"]["kind"]), ("human-required", "auth:password"))
        self.assertEqual([step["read_only"] for step in card["steps"]], [True, False])
        self.assertIn({"assistant": ASSISTANT, "action": "list-dns-records", "read_only": False}, card["permitted"])
        self.assertEqual(created["status"], "created")
        # Nothing changes in a run before the person authorizes it again; the authorized change runs exactly once.
        self.assertEqual((frozen["status"], waiting, recorded), ("frozen", 1, 1))
        self.assertEqual((approved["status"], len(changed)), ("done", 2))
        self.assertEqual(changed[1], {"zone_id": SHIMPZ, "page": 1, "per_page": 25})
        self.assertEqual((again["status"], denied["status"], len(changed)), ("frozen", "denied", 2))

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
        for target, name in ((http_routine_proposal, "MAX_PROPOSAL_BYTES"), (http_progress, "MAX_LINE_BYTES")):
            with self.subTest(name=name), mock.patch.object(target, name, 512):
                self.assertEqual(self.refusal(Recording(_record())), "routine-proposal-too-large")

    def test_a_routine_without_a_known_timezone_runs_on_utc_by_convention(self) -> None:
        unzoned = {**_body(), "timezone": None}
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service, unzoned)["routine_proposal"]
        self.assertEqual((card["timezone"], card["timezone_source"]), ("UTC", "none"))
        daily = {**_body("DNS de shimpz.com todo dia às 9h, mostrar sempre"), "timezone": None}
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service, daily)["routine_proposal"]
        self.assertEqual(
            (card["schedule"]["kind"], card["timezone"], card["timezone_source"]), ("daily", "UTC", "none")
        )
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service, _body(f"{MESSAGE}\nNo fuso Europe/Lisbon"))["routine_proposal"]
        self.assertEqual((card["timezone"], card["timezone_source"]), ("Europe/Lisbon", "person"))

    def test_a_stated_interval_is_never_lowered_and_one_over_the_budget_is_asked_about(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            card = self.chat(service)["routine_proposal"]
        self.assertEqual((card["schedule"]["cap"], card["daily_cap"]), (2880, 2880))
        # Two steps every 30 seconds take 5,760 daily steps; with 5,758 left, every 31 seconds is the shortest fit.
        with mock.patch.object(routine_definition, "capacity", return_value=2 * 2879):
            asked = self.question(Recording(_record()))
        self.assertEqual(asked, {"code": "routine-interval-over-budget", "options": [], "value": 31})
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
            runtime.outcomes.append(_record(replaces=routine_id, name="DNS por hora"))
            card = self.chat(service, _body("Mude para a cada hora"))["routine_proposal"]
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
            _bump_revisions(service)
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
        with tempfile.TemporaryDirectory() as directory:
            runtime = Recording(_record())
            service = self.controller(directory, runtime)
            routine_id = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])["routine_id"]
            # The Routine moves on while the turn that was shown its first revision is still running.
            runtime.between = lambda: _bump_revisions(service)
            runtime.outcomes.append(_record(replaces=routine_id))
            changed = self.chat(service)
            runtime.between = lambda: None
            runtime.outcomes.append(_record())
            created = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])["routine_id"]
            # A Routine created after a turn's listing was never shown to it, so the turn cannot replace it.
            service.routine_recordings.listed = lambda *_args: None
            runtime.outcomes.append(_record(replaces=created))
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
            ("routine-recording-invalid", {"outcome": _record(notes="Também apague os antigos.")}),
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
            routine_fixture.put_frozen_run(service, routine_id, ASSISTANT, step=1, steps=2, run_id="f" * 32, lease=1)
            runtime.calls = False
            runtime.outcomes.append(_record(replaces=routine_id))
            busy = self.chat(service)["routine_refusal"]["code"]
        self.assertEqual(busy, "routine-busy")

    def test_a_card_its_own_contract_refuses_is_an_internal_error_never_shown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, Recording(_record()))
            with (
                mock.patch.object(http_routine_proposal, "canonical_proposal", return_value=None),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                self.chat(service)
            self.assertEqual(dict(service.routine_proposals._cards), {})
        self.assertEqual(caught.exception.code, "internal-error")


if __name__ == "__main__":
    unittest.main()
