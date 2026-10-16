"""A person's answer to Team's Routine question: who reads it, the work it keeps, and what it records (ADR-0101)."""

import json
import tempfile

from test_local_routine_proposal import CONTINUOUS, SHIMPZ, RecordingCase, Sends, _body, _bump_revisions, _record

from local.routine import manage as routine_manage
from local.routine import recorder as local_routine_recorder
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine_proposal as http_routine_proposal


class RoutineAnswerTests(RecordingCase):
    def test_a_question_keeps_the_span_and_the_persons_answer_completes_the_card(self) -> None:
        runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body("Liste os registros DNS de shimpz.com e mostre o resultado"))[
                "routine_question"
            ]
            card = self.chat(service, _body("A cada 30 segundos"))["routine_proposal"]
            span = service.routine_recordings._spans.get("team_1")
            schedule = self.candidate(service, card).schedule
        self.assertEqual(asked, {"code": "routine-schedule-unstated", "options": [], "value": None})
        self.assertEqual(
            (schedule, [step["action"] for step in card["steps"]]), (CONTINUOUS, ["list-zones", "list-dns-records"])
        )
        # The card ended the span.
        self.assertIsNone(span)

    def test_a_schedule_answer_reaches_the_brain_which_records_the_work_again(self) -> None:
        original = "Liste os registros DNS de shimpz.com e mostre o resultado"
        work = ("list-zones", "list-dns-records")
        # The offered answer is a settings answer: the Brain runs nothing again and records the work that already ran.
        runtime = Sends((work, _record()), ((), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body(original))["routine_question"]
            answer = http_payload.compose_clarified(original, "Com que frequência?", "A cada 30 segundos", "pt")
            response = self.chat(service, _body(answer))
            candidate = self.candidate(service, response["routine_proposal"])
        self.assertEqual(asked["code"], "routine-schedule-unstated")
        self.assertEqual([context.routine_settings_answer for context in runtime.contexts], [False, True])
        self.assertEqual(candidate.schedule, CONTINUOUS)
        self.assertEqual([step["action"] for step in candidate.plan["steps"]], list(work))

    def test_an_output_answer_reaches_the_brain_which_records_the_earlier_work_again(self) -> None:
        original = "Liste os registros DNS de shimpz.com a cada 30 segundos"
        said = "Lê os registros DNS de shimpz.com."
        # The answer runs no Action again: the Brain only records once more, and the card carries that record.
        runtime = Sends((("list-zones", "list-dns-records"), _record()), ((), _record(description=said)))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body(original))["routine_question"]
            choice = http_routine_proposal.OUTPUT_CHOICES["pt"]["changes"]
            answer = http_payload.compose_clarified(original, "O que fazer com o resultado?", choice, "pt")
            card = self.chat(service, _body(answer))["routine_proposal"]
            candidate = self.candidate(service, card)
        self.assertEqual(asked, {"code": "routine-output-unstated", "options": [], "value": None})
        self.assertEqual([context.routine_output for context in runtime.contexts], [None, "changes"])
        self.assertEqual([context.routine_settings_answer for context in runtime.contexts], [False, True])
        self.assertEqual(runtime.contexts[1].routine_question, asked)
        self.assertEqual((candidate.plan["output"]["mode"], card["description"]), ("changes", said))
        self.assertEqual([step["action"] for step in candidate.plan["steps"]], ["list-zones", "list-dns-records"])

    def test_an_answer_keeps_the_work_its_record_call_selected_after_an_unrelated_send(self) -> None:
        original = "Liste os registros DNS de shimpz.com a cada 30 segundos"
        # Between the question and its answer the person asks for one zone listing that is no part of the Routine.
        runtime = Sends((("list-zones", "list-dns-records"), _record()), (("list-zones",), None), ((), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body(original))["routine_question"]
            self.chat(service, _body("Agora só liste as zonas uma vez."))
            choice = http_routine_proposal.OUTPUT_CHOICES["pt"]["changes"]
            answer = http_payload.compose_clarified(original, "O que fazer com o resultado?", choice, "pt")
            candidate = self.candidate(service, self.chat(service, _body(answer))["routine_proposal"])
        self.assertEqual(asked["code"], "routine-output-unstated")
        self.assertEqual([step["action"] for step in candidate.plan["steps"]], ["list-zones", "list-dns-records"])

    def test_a_settings_answer_keeps_the_stored_work_even_when_the_brain_renames_the_routine(self) -> None:
        original = "Liste os registros DNS de shimpz.com a cada 30 segundos"
        said = "Lê os registros DNS de shimpz.com."
        # An unrelated send lists the zones; the settings answer's record call then names the Routine otherwise.
        renamed = _record(name="Registros DNS", description=said)
        runtime = Sends((("list-zones", "list-dns-records"), _record()), (("list-zones",), None), ((), renamed))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            self.chat(service, _body(original))
            self.chat(service, _body("Agora só liste as zonas uma vez."))
            choice = http_routine_proposal.OUTPUT_CHOICES["pt"]["changes"]
            answer = http_payload.compose_clarified(original, "O que fazer com o resultado?", choice, "pt")
            card = self.chat(service, _body(answer))["routine_proposal"]
        self.assertEqual((card["name"], card["replaces"], card["description"]), (_record()["name"], None, said))
        self.assertEqual([step["action"] for step in card["steps"]], ["list-zones", "list-dns-records"])

    def test_a_typed_answer_that_runs_nothing_keeps_the_stored_work_under_a_new_name(self) -> None:
        # An unrelated send lists the zones; the typed answer's record call then names the Routine otherwise.
        renamed = _record(name="Registros DNS")
        runtime = Sends((("list-zones", "list-dns-records"), _record()), (("list-zones",), None), ((), renamed))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            asked = self.chat(service, _body("Liste os registros DNS de shimpz.com e mostre o resultado"))[
                "routine_question"
            ]
            self.chat(service, _body("Agora só liste as zonas uma vez."))
            card = self.chat(service, _body("A cada 30 segundos"))["routine_proposal"]
        self.assertEqual(asked["code"], "routine-schedule-unstated")
        self.assertEqual((card["name"], card["replaces"]), ("Registros DNS", None))
        self.assertEqual([step["action"] for step in card["steps"]], ["list-zones", "list-dns-records"])

    def test_a_typed_answer_that_runs_nothing_keeps_the_revision_its_replacement_was_shown(self) -> None:
        original = "Liste os registros DNS"
        runtime = Sends((("list-zones", "list-dns-records"), _record()))
        with tempfile.TemporaryDirectory() as directory:
            service = self.controller(directory, runtime)
            created = self.confirm(service, self.chat(service)["routine_proposal"]["proposal_id"])["routine_id"]
            runtime.scripts.append((("list-zones", "list-dns-records"), _record(replaces=created)))
            runtime.scripts.append(((), _record(name="Registros DNS", replaces=created)))
            asked = self.chat(service, _body(original))["routine_question"]
            _bump_revisions(service)
            # The answer's turn is shown the newer revision, which its record call never saw the work against.
            refusal = self.chat(service, _body("shimpz.com"))["routine_refusal"]
        self.assertEqual((asked["code"], refusal["code"]), ("routine-binding-ambiguous", "routine-revision-changed"))

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
                if not brain:
                    # The card Team records itself says everything: its reply carries no text.
                    self.assertEqual(response["reply"], "")

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
        self.assertEqual(asked["routine_question"]["code"], "routine-work-rerun")
        self.assertEqual(
            (zones["routine_proposal"]["name"], [step["action"] for step in zones["routine_proposal"]["steps"]]),
            ("Zonas", ["list-zones"]),
        )
        # The same intent recorded again keeps waiting for the verified rerun.
        self.assertEqual(again["routine_question"]["code"], "routine-work-rerun")
        self.assertEqual(continued["routine_question"]["code"], "routine-work-rerun")

    def test_an_answer_whose_selected_work_left_the_span_refuses_to_record_again(self) -> None:
        """Neither a target answer Team records itself nor the Brain's record falls back to other steps."""
        original = "Liste os registros DNS a cada 30 segundos e mostre o resultado"
        for brain in (False, True):
            # Between the question and its answer, sends that list the zones push the selected work out of the span.
            later = [(("list-zones",), None)] * local_routine_recorder.MAX_SENDS
            answered = ((), _record()) if brain else ((), None)
            runtime = Sends((("list-zones", "list-dns-records"), _record()), *later, answered)
            with self.subTest(brain=brain), tempfile.TemporaryDirectory() as directory:
                service = self.controller(directory, runtime)
                asked = self.chat(service, _body(original))["routine_question"]
                for _ in later:
                    self.chat(service, _body("Liste as zonas."))
                answer = "shimpz.com" if brain else json.dumps(SHIMPZ)
                composed = http_payload.compose_clarified(original, "Qual zona?", answer, "pt")
                response = self.chat(service, _body(composed))
                self.assertEqual(asked["code"], "routine-binding-ambiguous")
                self.assertEqual(len(runtime.contexts), len(later) + 1 + brain)
                self.assertEqual(response["routine_refusal"], {"code": "routine-record-again"})
