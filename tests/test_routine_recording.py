"""Team's deterministic recording of a Routine from a person's recent sends, with no model (ADR-0101).

Each top-level input member of a plan call is classified by the first rule that applies: a secret refuses, a value the
person named is a literal, the send's date in the Routine's zone is the run date, a value one result holds is copied
from that one occurrence, and anything else is a literal the assistant chose; what cannot be read is asked, never
guessed.
"""

import datetime
import json
import unittest

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine_proposal as http_routine_proposal
from routine import compose as routine_compose
from routine import plan as routine_plan
from routine import recording, trace

PIN = "sha256:" + "a" * 64
OTHER_PIN = "sha256:" + "b" * 64
DRIFTED_PIN = "sha256:" + "c" * 64
ZONE_ITEM = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "created_on": {"type": "string"}},
}
ZONES_OUT = {"type": "object", "properties": {"result": {"type": "array", "items": ZONE_ITEM}}}
RECORDS_IN = {
    "type": "object",
    "properties": {"zone_id": {"type": "string"}, "type": {"type": "string"}, "per_page": {"type": "integer"}},
    "required": ["zone_id"],
}
DELETE_IN = {
    "type": "object",
    "properties": {"zone_id": {"type": "string"}, "record_id": {"type": "string"}, "api_token": {"type": "string"}},
}
OPEN_IN = {"type": "object"}
CONTRACTS = {
    ("cloudflare", "list-zones"): routine_plan.ActionContract(PIN, OPEN_IN, read_only=True),
    ("cloudflare", "list-dns-records"): routine_plan.ActionContract(PIN, RECORDS_IN, read_only=True),
    ("cloudflare", "delete-dns-record"): routine_plan.ActionContract(OTHER_PIN, DELETE_IN),
    ("cloudflare", "renew-certificate"): routine_plan.ActionContract(OTHER_PIN, OPEN_IN),
    ("reports", "fetch"): routine_plan.ActionContract(PIN, OPEN_IN, read_only=True),
    ("reports", "post"): routine_plan.ActionContract(OTHER_PIN, OPEN_IN),
}
STARTED = int(datetime.datetime(2026, 10, 5, 15, tzinfo=datetime.UTC).timestamp())
SHIMPZ_ID = "023e105f4ecef8ad9ca31a8372d0c353"
TWIN_ID = "7f1b2c3d4e5f60718293a4b5c6d7e8f9"
ZONES = {
    "result": [
        {"id": "9a7806061c88ada191ed06f989cc3dac", "name": "example.com", "created_on": "2026-01-01"},
        {"id": "1b3f0c5e2a9d47e8b6c1d0f2a3b4c5d6", "name": "other.org", "created_on": "2026-01-02"},
        {"id": "5d41402abc4b2a76b9719d911017c592", "name": "blog.dev", "created_on": "2026-01-03"},
        {"id": SHIMPZ_ID, "name": "shimpz.com", "created_on": "2026-01-04"},
    ]
}
TWINS = {"result": [*ZONES["result"], {"id": TWIN_ID, "name": "shimpz.com", "created_on": "2026-01-05"}]}
EVERY_HOUR = "A cada hora"
OWNER = http_payload.compose_clarified(
    http_payload.compose_clarified(
        http_payload.compose_clarified(
            "Cria uma rotina pra mim", "O que a rotina deve fazer?", "Listar registros DNS", "pt"
        ),
        "Com que frequência?",
        "A cada 30 segundos",
        "pt",
    ),
    "De qual zona?",
    "shimpz.com",
    "pt",
)
ZONES_CALL = ("cloudflare/list-zones", {}, ZONES)
RECORDS = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {"result": [{"type": "A"}]})
_SEQUENCE = iter(range(10**9))


def _occurrence(call: tuple, started_at: int) -> trace.Occurrence:
    """One successful call: (assistant/action, input, result[, kept input])."""
    name, given, result = call[:3]
    assistant, action = name.split("/")
    contract = CONTRACTS[(assistant, action)]
    kept_input = call[3] if len(call) > 3 else trace.keep(given, contract.input_schema, ())
    output_schema = ZONES_OUT if action == "list-zones" else {}
    return trace.Occurrence(
        operation_id=f"6f1c2b8e-3a4d-4c5e-9f60-{next(_SEQUENCE):012d}",
        assistant=assistant,
        action=action,
        pin=contract.pin,
        read_only=contract.read_only,
        dispatched_at=started_at,
        input=kept_input,
        result=result if isinstance(result, trace.Kept) else trace.keep(result, output_schema, ()),
    )


def _send(
    *calls: tuple,
    message: str = EVERY_HOUR,
    window: tuple[str, ...] = (),
    timezone: str | None = "America/Sao_Paulo",
    started_at: int = STARTED,
) -> recording.Send:
    lines = tuple(
        line for entry in window for segment in http_payload.authored_segments(entry) for line in segment.split("\n")
    )
    occurrences = tuple(_occurrence(call, started_at) for call in calls)
    return recording.Send(message, http_payload.authored_segments(message), lines, timezone, started_at, occurrences)


def _then_hourly(text: str) -> str:
    """A request, then the person's answer that it runs every hour, as Admin composes it."""
    return http_payload.compose_clarified(text, "Com que frequência?", EVERY_HOUR, "pt")


def _record(*sends: recording.Send, mode: str = "show", **options) -> recording.Recorded | recording.Question:
    """Record a span; options are ``protection``, ``contracts``, ``asked``, and the rest."""
    return routine_compose.record(
        sends,
        recording.Recording(mode),
        options.get("protection") or trace.Protection(),
        options.get("contracts", CONTRACTS),
        asked=options.get("asked"),
        existing=options.get("existing"),
        frontier=options.get("frontier", 0),
    )


def _recorded(*sends: recording.Send, **options) -> recording.Recorded:
    result = _record(*sends, **options)
    if not isinstance(result, recording.Recorded):
        raise AssertionError(f"asked {result}")
    return result


def _code(test: unittest.TestCase, call) -> str:
    with test.assertRaises((recording.RecordingError, routine_plan.PlanError)) as raised:
        call()
    return raised.exception.code


def _input(recorded: recording.Recorded, step: int = -1) -> dict[str, object]:
    return recorded.document["steps"][step]["input"]


def _actions(recorded: recording.Recorded) -> list[str]:
    return [step["action"] for step in recorded.document["steps"]]


SELECTED = {"kind": "step_output", "step": "s1", "pointer": "/result", "where": {"name": "shimpz.com"}, "item": "/id"}


class OwnerCaseTests(unittest.TestCase):
    def test_the_zone_id_is_selected_by_the_zone_name_the_person_gave(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, RECORDS, message=OWNER))
        self.assertEqual(
            recorded.document,
            {
                "version": routine_plan.VERSION,
                "timezone": "America/Sao_Paulo",
                "steps": [
                    {"id": "s1", "assistant": "cloudflare", "action": "list-zones", "pin": PIN, "input": {}},
                    {
                        "id": "s2",
                        "assistant": "cloudflare",
                        "action": "list-dns-records",
                        "pin": PIN,
                        "input": {"zone_id": SELECTED},
                    },
                ],
                "output": {"mode": "show", "step": "s2"},
            },
        )
        self.assertEqual(recorded.origins, {"s1": {}, "s2": {"zone_id": "selector"}})
        self.assertEqual(
            (recorded.schedule, recorded.timezone, recorded.timezone_source),
            ({"kind": "continuous", "gap": 30, "cap": 2880}, "America/Sao_Paulo", "browser"),
        )
        self.assertEqual([item["action"] for item in recorded.permitted], ["list-dns-records", "list-zones"])

    def test_the_zone_looked_up_in_an_earlier_send_is_still_the_source(self) -> None:
        recorded = _recorded(
            _send(ZONES_CALL, message="Cria uma rotina pra mim"),
            _send(message="Listar registros DNS"),
            _send(RECORDS, message="shimpz.com, a cada 30 segundos"),
        )
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])
        self.assertEqual(_input(recorded)["zone_id"], SELECTED)

    def test_a_lookup_after_its_use_is_still_its_source(self) -> None:
        # Primed: the zones were listed earlier and again after the records; unprimed: only after.
        primed = _recorded(
            _send(ZONES_CALL, message="Liste os registros DNS de shimpz.com"),
            _send(RECORDS, ZONES_CALL, message="Faça isso a cada hora"),
        )
        unprimed = _recorded(_send(RECORDS, ZONES_CALL, message="DNS de shimpz.com a cada hora"))
        for recorded in (primed, unprimed):
            with self.subTest(recorded=recorded):
                self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])
                self.assertEqual(_input(recorded)["zone_id"], SELECTED)
                self.assertEqual(recorded.document["output"]["step"], "s2")

    def test_a_remembered_id_no_result_holds_is_asked_about(self) -> None:
        self.assertEqual(
            _record(_send(RECORDS, message="shimpz.com a cada hora")), recording.Question("routine-binding-unsourced")
        )

    def test_two_zones_of_the_named_name_are_asked_about_with_both_targets(self) -> None:
        twins = ("cloudflare/list-zones", {}, TWINS)
        work = _send(twins, RECORDS, message="DNS de shimpz.com a cada hora")
        asked = _record(work)
        targets = ({"value": SHIMPZ_ID, "label": "shimpz.com"}, {"value": TWIN_ID, "label": "shimpz.com"})
        self.assertEqual(asked, recording.Question("routine-binding-ambiguous", targets))
        pending = recording.Asked(asked.code, 1, asked.pending)
        # Choosing the zone the work used, by its exact JSON text, makes it a literal the person named.
        chosen = _recorded(work, _send(message=json.dumps(SHIMPZ_ID)), asked=pending)
        self.assertEqual(
            (_input(chosen)["zone_id"], chosen.origins["s2"]["zone_id"]),
            ({"kind": "literal", "value": SHIMPZ_ID}, "request"),
        )
        # Naming the other one asks for the work again, with that zone.
        rerun = _record(work, _send(message=json.dumps(TWIN_ID)), asked=pending)
        self.assertEqual(rerun, recording.Question("routine-work-rerun"))

    def test_only_an_exact_answer_confirms_a_target_and_a_substring_never_does(self) -> None:
        items = {"items": [{"name": "same", "id": "target"}, {"name": "same", "id": "target-b"}]}
        work = _send(
            ("reports/fetch", {}, items),
            ("cloudflare/list-dns-records", {"zone_id": "target"}, {}),
            message=_then_hourly("same"),
        )
        asked = _record(work)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        pending = recording.Asked(asked.code, 1, asked.pending)
        # Naming the other target asks for the work again, though the work's own target is a substring of the answer.
        self.assertEqual(_record(work, _send(message='"target-b"'), asked=pending).code, "routine-work-rerun")
        # An answer that is no target's exact JSON text confirms none: the question stands.
        for answer in ("quero o target-b", "target", "target-b"):
            with self.subTest(answer=answer):
                self.assertEqual(_record(work, _send(message=answer), asked=pending).code, "routine-binding-ambiguous")
        recorded = _recorded(work, _send(message='"target"'), asked=pending)
        self.assertEqual(_input(recorded)["zone_id"]["value"], "target")

    def test_a_string_and_an_integer_target_with_the_same_digits_stay_distinct(self) -> None:
        digits = "12345678901234567890"
        items = {"items": [{"name": "same", "id": digits}, {"name": "same", "id": int(digits)}]}
        work = _send(
            ("reports/fetch", {}, items),
            ("cloudflare/list-dns-records", {"zone_id": digits}, {}),
            message=_then_hourly("same"),
        )
        asked = _record(work)
        self.assertEqual([item["value"] for item in asked.wire()["options"]], [json.dumps(digits), digits])
        pending = recording.Asked(asked.code, 1, asked.pending)
        self.assertEqual(_record(work, _send(message=digits), asked=pending).code, "routine-work-rerun")
        recorded = _recorded(work, _send(message=json.dumps(digits)), asked=pending)
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "literal", "value": digits})

    def test_one_answer_binds_every_occurrence_of_its_target_and_never_an_independent_one(self) -> None:
        twins = ("cloudflare/list-zones", {}, TWINS)
        other_id = ZONES["result"][1]["id"]
        other = ("cloudflare/list-dns-records", {"zone_id": other_id}, {"result": []})
        # Mixed: shimpz.com is ambiguous, other.org resolves by its own name.
        mixed = _send(twins, RECORDS, other, message="DNS de shimpz.com e other.org a cada hora")
        asked = _record(mixed)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        answered = _recorded(
            mixed, _send(message=json.dumps(SHIMPZ_ID)), asked=recording.Asked(asked.code, 1, asked.pending)
        )
        inputs = [step["input"]["zone_id"] for step in answered.document["steps"][1:]]
        self.assertEqual(inputs[0], {"kind": "literal", "value": SHIMPZ_ID})
        self.assertEqual(inputs[1]["where"], {"name": "other.org"})
        # Twin-only: both twins were used, so one answer binds both, and the other twin's call was the choice not taken.
        twin = ("cloudflare/list-dns-records", {"zone_id": TWIN_ID}, {"result": []})
        both = _send(twins, RECORDS, twin, message="DNS de shimpz.com a cada hora")
        asked = _record(both)
        pending = recording.Asked(asked.code, 1, asked.pending)
        kept = _recorded(both, _send(message=json.dumps(SHIMPZ_ID)), asked=pending)
        self.assertEqual([step["input"].get("zone_id") for step in kept.document["steps"][1:]], [inputs[0]])

    def test_a_choice_among_twins_the_work_both_read_keeps_the_chosen_call_and_runs_nothing_again(self) -> None:
        # From a live trace: the agent read the records of both zones named shimpz.com, the person chose one.
        twins = ("cloudflare/list-zones", {}, TWINS)
        twin = ("cloudflare/list-dns-records", {"zone_id": TWIN_ID}, {"result": []})
        work = _send(twins, RECORDS, twin, message="A cada hora, liste os registros DNS de shimpz.com")
        asked = _record(work)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        pending = recording.Asked(asked.code, 1, asked.pending)
        recorded = _recorded(work, _send(message=json.dumps(SHIMPZ_ID)), asked=pending)
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])
        self.assertEqual(
            (_input(recorded)["zone_id"], recorded.origins["s2"]["zone_id"]),
            ({"kind": "literal", "value": SHIMPZ_ID}, "request"),
        )
        self.assertEqual(recorded.document["output"]["step"], "s2")
        # Choosing the twin works the same way, in either order of the calls.
        reordered = _send(twins, twin, RECORDS, message="A cada hora, liste os registros DNS de shimpz.com")
        other = _recorded(reordered, _send(message=json.dumps(TWIN_ID)), asked=pending)
        self.assertEqual(_input(other)["zone_id"], {"kind": "literal", "value": TWIN_ID})

    def test_a_changing_call_on_a_target_the_person_did_not_choose_is_never_dropped(self) -> None:
        twins = ("cloudflare/list-zones", {}, TWINS)
        deletes = tuple(
            ("cloudflare/delete-dns-record", {"zone_id": zone, "record_id": "r" * 32}, {})
            for zone in (SHIMPZ_ID, TWIN_ID)
        )
        work = _send(twins, *deletes, message="A cada hora, apague o registro de shimpz.com")
        asked = _record(work)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        rerun = _record(work, _send(message=json.dumps(SHIMPZ_ID)), asked=recording.Asked(asked.code, 1, asked.pending))
        self.assertEqual(rerun.code, "routine-work-rerun")
        self.assertEqual(len(rerun.manifest.slots), 3)

    def test_every_answered_binding_stays_bound_through_later_binding_questions(self) -> None:
        other_id, other_twin = ZONES["result"][1]["id"], "8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5"
        zones = {"result": [*TWINS["result"], {"id": other_twin, "name": "other.org", "created_on": "2026-01-06"}]}
        listing = ("cloudflare/list-zones", {}, zones)
        other = ("cloudflare/list-dns-records", {"zone_id": other_id}, {"result": []})
        work = _send(listing, other, RECORDS, message="DNS de other.org e shimpz.com a cada hora")
        groups = {frozenset({other_id, other_twin}): other_id, frozenset({SHIMPZ_ID, TWIN_ID}): SHIMPZ_ID}
        first = _record(work)
        used = groups[frozenset(target for target, _label in first.pending.targets)]
        answer = _send(message=json.dumps(used))
        second = _record(work, answer, asked=_asked(first, 1))
        self.assertEqual(second.code, "routine-binding-ambiguous")
        remaining = groups[frozenset(target for target, _label in second.pending.targets)]
        self.assertNotEqual(remaining, used)
        answers = (answer, _send(message=json.dumps(remaining)))
        recorded = _recorded(work, *answers, asked=_asked(second, 2))
        self.assertEqual(
            {step["input"]["zone_id"]["value"] for step in recorded.document["steps"][1:]}, {other_id, SHIMPZ_ID}
        )
        # Work that later uses a twin the person did not choose, and never the one they chose, is run again.
        rejected = ("cloudflare/list-dns-records", {"zone_id": TWIN_ID if used == SHIMPZ_ID else other_twin}, {"n": 1})
        later = _send(listing, rejected)
        self.assertEqual(_record(work, *answers, later, asked=_asked(second, 2)).code, "routine-work-rerun")
        # Beside the chosen one, its call was the choice not taken.
        beside = _recorded(work, *answers, _send(listing, other, rejected, RECORDS), asked=_asked(second, 2))
        self.assertEqual(
            {step["input"]["zone_id"]["value"] for step in beside.document["steps"][1:]}, {other_id, SHIMPZ_ID}
        )

    def test_work_run_again_for_a_chosen_target_must_use_exactly_that_target(self) -> None:
        items = {"items": [{"name": "same", "id": "target"}, {"name": "same", "id": "target-b"}]}
        fetch = ("reports/fetch", {}, items)
        work = _send(fetch, ("cloudflare/list-dns-records", {"zone_id": "target"}, {}), message=_then_hourly("same"))
        asked = _record(work)
        rerun = _record(work, _send(message='"target-b"'), asked=recording.Asked(asked.code, 1, asked.pending))
        chosen = _asked(rerun, 2)
        again = _send(fetch, ("cloudflare/list-dns-records", {"zone_id": "target-b"}, {}))
        recorded = _recorded(work, _send(message='"target-b"'), again, asked=chosen)
        self.assertEqual(
            (_input(recorded)["zone_id"], recorded.origins["s2"]["zone_id"]),
            ({"kind": "literal", "value": "target-b"}, "request"),
        )
        wrong = _send(fetch, ("cloudflare/list-dns-records", {"zone_id": "target"}, {}))
        self.assertEqual(_record(work, _send(message='"target-b"'), wrong, asked=chosen).code, "routine-work-rerun")
        # A rerun that also sends the other target again repeats the work but contradicts the choice.
        both = _send(
            fetch, *(("cloudflare/list-dns-records", {"zone_id": zone}, {}) for zone in ("target-b", "target"))
        )
        self.assertEqual(_record(work, _send(message='"target-b"'), both, asked=chosen).code, "routine-work-rerun")
        # Once the rerun settled, later work that sends the other target again contradicts the choice.
        later = _send(fetch, ("cloudflare/list-dns-records", {"zone_id": "target"}, {"n": 1}))
        contradicted = _record(work, _send(message='"target-b"'), again, later, asked=chosen)
        self.assertEqual(contradicted.code, "routine-work-rerun")

    def test_a_zone_listed_without_a_name_is_never_read_by_its_position(self) -> None:
        # A reordered list would make a position target another zone, so the person is asked which one it is.
        first = ZONES["result"][0]
        call = ("cloudflare/list-dns-records", {"zone_id": first["id"]}, {"result": []})
        work = _send(ZONES_CALL, call, message="Liste os DNS a cada hora")
        asked = _record(work)
        targets = tuple({"value": item["id"], "label": item["name"]} for item in ZONES["result"])
        self.assertEqual(asked, recording.Question("routine-binding-ambiguous", targets))
        pending = recording.Asked(asked.code, 1, asked.pending)
        # Naming the zone's name selects its item by that name; choosing its id fixes the id the person chose.
        by_name = _recorded(work, _send(message=first["name"]), asked=pending)
        self.assertEqual(
            _input(by_name)["zone_id"],
            {
                "kind": "step_output",
                "step": "s1",
                "pointer": "/result",
                "where": {"name": first["name"]},
                "item": "/id",
            },
        )
        by_id = _recorded(work, _send(message=json.dumps(first["id"])), asked=pending)
        self.assertEqual(by_id.origins["s2"]["zone_id"], "request")
        other = _record(
            work,
            _send(message=json.dumps(ZONES["result"][1]["id"])),
            asked=pending,
        )
        self.assertEqual(other, recording.Question("routine-work-rerun"))


def _fields(item: trace.Occurrence) -> dict[str, object]:
    return {name: getattr(item, name) for name, spec in item.__dataclass_fields__.items() if spec.init}


def _asked(question: recording.Question, after: int) -> recording.Asked:
    """The span's record of a question asked after ``after`` sends, with every choice already answered."""
    return recording.Asked(
        question.code,
        after,
        question.pending,
        question.manifest,
        chosen=question.chosen,
        chained_from=question.chained_from,
    )


def _pending(question: recording.Question, after: int) -> recording.Asked:
    """The span's record of a question asked after ``after`` sends."""
    return recording.Asked(question.code, after, question.pending, question.manifest)


def _post(text: str) -> tuple:
    return ("reports/post", {"t": text}, {})


class KnownTextTests(unittest.TestCase):
    def test_a_name_from_an_earlier_send_of_the_conversation_anchors_the_selector(self) -> None:
        call = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID, "per_page": 50}, {"result": []})
        recorded = _recorded(
            _send(
                ZONES_CALL,
                call,
                message="Faça isso a cada hora",
                window=("Liste os registros DNS de shimpz.com, 50 por página",),
            )
        )
        self.assertEqual(
            (_input(recorded)["zone_id"]["where"], recorded.origins["s2"]["zone_id"]),
            ({"name": "shimpz.com"}, "selector"),
        )
        self.assertEqual(recorded.origins["s2"]["per_page"], "request")

    def test_no_name_or_number_spans_two_lines(self) -> None:
        call = ("reports/fetch", {"site": "ab.cd", "port": 8443}, {})
        recorded = _recorded(_send(call, message=f"ab.\ncd 84\n43 hoje\n{EVERY_HOUR}"))
        self.assertEqual(recorded.origins["s1"], {"site": "assistant", "port": "assistant"})

    def test_a_question_line_names_nothing(self) -> None:
        composed = http_payload.compose_clarified(
            "Liste os registros", "Da zona shimpz-zone-1?", "sim, a cada hora", "pt"
        )
        call = ("reports/fetch", {"site": "shimpz-zone-1"}, {})
        self.assertEqual(_record(_send(call, message=composed)).code, "routine-binding-unsourced")


class ScheduleTests(unittest.TestCase):
    def test_the_latest_send_stating_a_schedule_wins(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="todo dia às 9h"), _send(message="melhor a cada 30 segundos"))
        self.assertEqual(recorded.schedule, {"kind": "continuous", "gap": 30, "cap": 2880})

    def test_different_schedules_in_one_authored_text_are_asked_about_even_across_lines(self) -> None:
        asked = _record(_send(ZONES_CALL, message="Run every 5 seconds.\nRun every 30 seconds."))
        self.assertEqual(asked, recording.Question("routine-schedule-unstated"))

    def test_an_answer_in_a_composed_send_wins_over_the_request_it_repeats(self) -> None:
        composed = http_payload.compose_clarified("DNS a cada 5 segundos", "Tem certeza?", "a cada 30 segundos", "pt")
        recorded = _recorded(_send(ZONES_CALL, message=composed))
        self.assertEqual(recorded.schedule, {"kind": "continuous", "gap": 30, "cap": 2880})

    def test_an_unreadable_count_is_asked_through_the_schedule_question(self) -> None:
        asked = _record(_send(ZONES_CALL, message="every " + "9" * 5000 + " seconds"))
        self.assertEqual(asked, recording.Question("routine-schedule-unstated"))

    def test_no_schedule_two_in_one_text_or_one_only_asked_are_asked_again(self) -> None:
        for message in ("Cria uma rotina", "a cada hora e todo dia às 9h", "Todo dia às 9h?"):
            with self.subTest(message=message):
                self.assertEqual(
                    _record(_send(ZONES_CALL, message=message)), recording.Question("routine-schedule-unstated")
                )

    def test_a_replacement_keeps_its_schedule_unless_one_is_stated(self) -> None:
        existing = recording.Existing({"steps": []}, {"kind": "daily", "time": "08:00"})
        kept = _recorded(_send(ZONES_CALL, message="sem mudar o horário"), existing=existing)
        self.assertEqual(kept.schedule, {"kind": "daily", "time": "08:00"})
        changed = _recorded(_send(ZONES_CALL, message="a cada hora"), existing=existing)
        self.assertEqual(changed.schedule, {"kind": "hourly", "every": 1})


class OutputTests(unittest.TestCase):
    """What each run does with its result is the person's to state, read from their words (ADR-0101)."""

    def output(self, *sends: recording.Send, **options) -> object:
        result = _record(*sends, mode=None, **options)
        return result.document["output"] if isinstance(result, recording.Recorded) else result

    def test_the_latest_segment_stating_an_output_wins_and_a_chain_shows(self) -> None:
        cases = {
            "a cada hora, mostrar sempre": {"mode": "show", "step": "s1"},
            "a cada hora, só quando mudar": {"mode": "changes", "step": "s1"},
            "a cada hora, não precisa mostrar": {"mode": "none", "step": None},
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(self.output(_send(ZONES_CALL, message=message)), expected)
        later = self.output(_send(ZONES_CALL, message="a cada hora, mostrar sempre"), _send(message="só quando mudar"))
        self.assertEqual(later["mode"], "changes")

    def test_a_chain_records_only_work_that_uses_an_earlier_result(self) -> None:
        chained = _send(ZONES_CALL, RECORDS, message="DNS de shimpz.com a cada hora, usar em outras ações")
        self.assertEqual(self.output(chained)["mode"], "show")
        # A lookup that nothing else consumes is no chain: the output question stands.
        alone = _send(ZONES_CALL, message="zonas a cada hora, usar em outras ações")
        self.assertEqual(self.output(alone), recording.Question("routine-output-unstated"))

    def test_a_chain_chosen_after_the_question_must_use_the_result_it_asked_about(self) -> None:
        records = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {"result": {"id": "rec-1234567"}})
        work = _send(ZONES_CALL, records, message="DNS de shimpz.com a cada hora")
        asked = _record(work, mode=None)
        self.assertEqual(
            (asked.code, asked.chained_from[:2]), ("routine-output-unstated", ("cloudflare", "list-dns-records"))
        )
        chain = _send(message=http_routine_proposal.OUTPUT_CHOICES["pt"]["chain"])
        # Recording again with no new Action, or repeating the same work, uses no result of the records listing.
        for later in ((), (_send(ZONES_CALL, records),)):
            with self.subTest(later=len(later)):
                again = _record(work, chain, *later, asked=_asked(asked, 1), mode=None)
                self.assertEqual(
                    (again.code, again.chained_from[:2]),
                    ("routine-output-unstated", ("cloudflare", "list-dns-records")),
                )
        post = ("reports/post", {"record": "rec-1234567"}, {})
        chained = _recorded(work, chain, _send(ZONES_CALL, records, post), asked=_asked(asked, 1), mode=None)
        self.assertEqual(_actions(chained), ["list-zones", "list-dns-records", "post"])
        self.assertEqual(chained.document["output"]["mode"], "show")

    def test_a_chain_must_use_the_shown_call_itself_not_another_call_of_its_action(self) -> None:
        alpha = ("reports/fetch", {"q": "alpha"}, {"id": "aaa-111111"})
        beta = ("reports/fetch", {"q": "beta"}, {"id": "bbb-222222"})
        work = _send(alpha, beta, message="relatório a cada hora")
        asked = _record(work, mode=None)
        self.assertEqual(asked.code, "routine-output-unstated")
        chain = _send(message=http_routine_proposal.OUTPUT_CHOICES["pt"]["chain"])
        wrong = _send(alpha, beta, ("reports/post", {"x": "aaa-111111"}, {}))
        self.assertEqual(_record(work, chain, wrong, asked=_asked(asked, 1), mode=None).code, asked.code)
        right = _send(alpha, beta, ("reports/post", {"x": "bbb-222222"}, {}))
        self.assertIsInstance(_record(work, chain, right, asked=_asked(asked, 1), mode=None), recording.Recorded)

    def test_a_chain_must_use_the_shown_result_of_identical_calls(self) -> None:
        alpha = ("reports/fetch", {"q": "stats"}, {"id": "aaa-111111"})
        beta = ("reports/fetch", {"q": "stats"}, {"id": "bbb-222222"})
        work = _send(alpha, beta, message="relatório a cada hora")
        asked = _record(work, mode=None)
        chain = _send(message=http_routine_proposal.OUTPUT_CHOICES["pt"]["chain"])
        first = _send(alpha, beta, ("reports/post", {"x": "aaa-111111"}, {}))
        self.assertEqual(_record(work, chain, first, asked=_asked(asked, 1), mode=None).code, asked.code)
        second = _send(alpha, beta, ("reports/post", {"x": "bbb-222222"}, {}))
        self.assertIsInstance(_record(work, chain, second, asked=_asked(asked, 1), mode=None), recording.Recorded)

    def test_no_output_or_two_in_one_segment_are_asked_never_guessed(self) -> None:
        for message in ("a cada hora", "a cada hora. Mostrar sempre. Ou só quando mudar."):
            with self.subTest(message=message):
                self.assertEqual(
                    self.output(_send(ZONES_CALL, message=message)), recording.Question("routine-output-unstated")
                )
        # The work's own questions come first.
        remembered = _send(RECORDS, message="DNS de shimpz.com a cada hora")
        self.assertEqual(self.output(remembered).code, "routine-binding-unsourced")

    def test_a_replacement_keeps_its_output_unless_the_person_states_another(self) -> None:
        existing = recording.Existing(KEPT_PLAN, {"kind": "daily", "time": "08:00"})
        self.assertEqual(self.output(_send(message="ok"), existing=existing)["mode"], "show")
        changed = self.output(_send(message="não precisa mostrar"), existing=existing)
        self.assertEqual(changed["mode"], "none")
        asked = self.output(_send(message="Mostrar sempre. Ou só quando mudar."), existing=existing)
        self.assertEqual(asked, recording.Question("routine-output-unstated"))


class TimezoneTests(unittest.TestCase):
    def test_a_written_zone_wins_over_the_browser_and_the_latest_one_counts(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="todo dia às 9h, Europe/Paris"), _send(message="Europe/Lisbon"))
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("Europe/Lisbon", "person"))
        self.assertEqual(recorded.document["timezone"], "Europe/Lisbon")

    def test_a_zone_ending_a_sentence_is_the_persons_and_a_partial_offset_is_no_zone(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="Run daily at 09:00 in Europe/London."))
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("Europe/London", "person"))
        offset = _recorded(_send(ZONES_CALL, message="Run daily at 09:00 UTC+3"))
        self.assertEqual((offset.timezone, offset.timezone_source), ("America/Sao_Paulo", "browser"))

    def test_two_zones_in_the_latest_naming_segment_name_none_and_a_later_single_zone_wins(self) -> None:
        two = _send(ZONES_CALL, message="todo dia às 9h, Europe/Paris ou Europe/London")
        unnamed = _recorded(two)
        self.assertEqual((unnamed.timezone, unnamed.timezone_source), ("America/Sao_Paulo", "browser"))
        answered = _recorded(two, _send(message="Europe/London"))
        self.assertEqual((answered.timezone, answered.timezone_source), ("Europe/London", "person"))

    def test_a_composed_answer_settles_two_zones_its_request_repeats(self) -> None:
        original = "DNS todo dia às 9h, Europe/Paris or Europe/London"
        composed = http_payload.compose_clarified(original, "Qual fuso?", "Europe/London", "en")
        answered = _recorded(_send(ZONES_CALL, message=composed))
        self.assertEqual((answered.timezone, answered.timezone_source), ("Europe/London", "person"))

    def test_the_latest_browser_zone_counts(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="todo dia às 9h", timezone="Asia/Tokyo"), _send(message="ok"))
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("America/Sao_Paulo", "browser"))

    def test_with_no_zone_known_it_runs_on_utc_by_convention(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="a cada hora", timezone=None))
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("UTC", "none"))
        daily = _recorded(_send(ZONES_CALL, message="todo dia às 9h", timezone=None))
        self.assertEqual((daily.timezone, daily.timezone_source, daily.document["timezone"]), ("UTC", "none", "UTC"))
        unzoned = recording.Existing({"steps": []}, {"kind": "hourly", "every": 1})
        self.assertEqual(_recorded(_send(ZONES_CALL, timezone="Asia/Tokyo"), existing=unzoned).timezone, "Asia/Tokyo")


class ReplacedZoneTests(unittest.TestCase):
    def test_a_replacement_takes_the_requests_zone_never_the_replaced_routines(self) -> None:
        london = recording.Existing({"steps": []}, {"kind": "daily", "time": "08:00"})
        tokyo = _recorded(_send(ZONES_CALL, message="sem mudar o horário", timezone="Asia/Tokyo"), existing=london)
        self.assertEqual((tokyo.timezone, tokyo.timezone_source), ("Asia/Tokyo", "browser"))
        unzoned = _recorded(_send(ZONES_CALL, message="sem mudar o horário", timezone=None), existing=london)
        self.assertEqual((unzoned.timezone, unzoned.timezone_source), ("UTC", "none"))


class BoundaryTests(unittest.TestCase):
    def test_every_work_call_replays_in_order(self) -> None:
        recorded = _recorded(
            _send(
                ("reports/fetch", {}, {"unused": 1}),
                ZONES_CALL,
                ("reports/post", {"text": "hello"}, {"ok": True}),
                ("reports/fetch", {"q": 1}, {}),
                RECORDS,
                message=f"shimpz.com\n{EVERY_HOUR}",
            ),
            mode="changes",
        )
        self.assertEqual(_actions(recorded), ["fetch", "list-zones", "post", "fetch", "list-dns-records"])
        self.assertEqual(_input(recorded)["zone_id"]["step"], "s2")
        self.assertEqual(recorded.document["output"], {"mode": "changes", "step": "s5"})

    def test_none_mode_shows_nothing(self) -> None:
        recorded = _recorded(_send(("reports/fetch", {}, {})), mode="none")
        self.assertEqual(recorded.document["output"], {"mode": "none", "step": None})

    def test_a_recording_needs_a_call(self) -> None:
        self.assertEqual(_code(self, lambda: _record(_send())), "routine-recording-empty")

    def test_the_output_mode_is_closed_and_a_retired_decision_is_refused(self) -> None:
        send = _send(("reports/fetch", {}, {}))
        cases = [({"mode": "decide"}, "routine-recording-invalid"), ({"mode": "chain"}, "routine-recording-invalid")]
        for options, code in cases:
            with self.subTest(options=options):
                self.assertEqual(_code(self, lambda o=options: _record(send, **o)), code)

    def test_a_drifted_or_unknown_contract_fails_closed(self) -> None:
        drifted = {
            **CONTRACTS,
            ("cloudflare", "delete-dns-record"): routine_plan.ActionContract(DRIFTED_PIN, DELETE_IN),
        }
        send = _send(("reports/fetch", {}, {}), ("cloudflare/delete-dns-record", {"zone_id": "z"}, {}))
        self.assertEqual(_code(self, lambda: _record(send, contracts=drifted)), "plan-pin-drift")
        missing = {key: value for key, value in CONTRACTS.items() if key != ("reports", "fetch")}
        self.assertEqual(_code(self, lambda: _record(send, contracts=missing)), "plan-pin-drift")


KEPT_PLAN = {
    "version": routine_plan.VERSION,
    "timezone": "UTC",
    "steps": [
        {"id": "s1", "assistant": "cloudflare", "action": "list-zones", "pin": PIN, "input": {}},
        {
            "id": "s2",
            "assistant": "cloudflare",
            "action": "list-dns-records",
            "pin": PIN,
            "input": {
                "zone_id": {
                    "kind": "step_output",
                    "step": "s1",
                    "pointer": "/result",
                    "where": {"name": "a.com"},
                    "item": "/id",
                },
                "type": {"kind": "literal", "value": "MX"},
                "per_page": {"kind": "literal", "value": 50},
            },
        },
    ],
    "output": {"mode": "show", "step": "s2"},
}


class KeptTests(unittest.TestCase):
    """A replacement that ran no Action keeps the replaced plan's steps exactly (ADR-0101)."""

    def keep(self, mode: str = "changes", **options) -> recording.Recorded | recording.Question:
        existing = recording.Existing(options.get("plan", KEPT_PLAN), {"kind": "daily", "time": "08:00"})
        return _record(
            _send(message=options.get("message", "now with 50 per page")),
            mode=mode,
            protection=options.get("protection"),
            existing=existing,
        )

    def test_the_steps_stay_and_only_how_and_when_they_run_change(self) -> None:
        kept = self.keep()
        self.assertEqual(kept.document["steps"], KEPT_PLAN["steps"])
        self.assertEqual(
            (kept.document["timezone"], kept.schedule), ("America/Sao_Paulo", {"kind": "daily", "time": "08:00"})
        )
        self.assertEqual(kept.document["output"], {"mode": "changes", "step": "s2"})
        self.assertEqual(
            kept.origins, {"s1": {}, "s2": {"zone_id": "selector", "type": "assistant", "per_page": "request"}}
        )
        self.assertEqual([item["action"] for item in kept.permitted], ["list-dns-records", "list-zones"])
        plain = {
            **KEPT_PLAN,
            "steps": [
                KEPT_PLAN["steps"][0],
                {
                    **KEPT_PLAN["steps"][1],
                    "input": {
                        "zone_id": {"kind": "step_output", "step": "s1", "pointer": "/zone"},
                        "day": {"kind": "run_clock", "format": "date"},
                    },
                },
            ],
        }
        self.assertEqual(self.keep(plan=plain).origins["s2"], {"zone_id": "step", "day": "clock"})
        self.assertEqual(self.keep(message="a cada hora").schedule, {"kind": "hourly", "every": 1})

    def test_a_lost_protection_or_a_drifted_pin_refuses(self) -> None:
        drifted = {**KEPT_PLAN, "steps": [{**KEPT_PLAN["steps"][0], "pin": DRIFTED_PIN}]}
        cases = [
            ({"protection": trace.Protection(lost=True)}, "routine-recording-unavailable"),
            ({"plan": drifted}, "plan-pin-drift"),
        ]
        for options, code in cases:
            with self.subTest(code=code):
                self.assertEqual(_code(self, lambda o=options: self.keep(**o)), code)


class QuestionTests(unittest.TestCase):
    def test_a_question_carries_each_target_as_its_exact_json_text(self) -> None:
        question = recording.Question(
            "routine-binding-ambiguous", ({"value": "a", "label": None}, {"value": 2**53 + 1, "label": "big"})
        )
        wire = question.wire()
        self.assertEqual(
            wire,
            {
                "code": "routine-binding-ambiguous",
                "options": [{"value": '"a"', "label": None}, {"value": "9007199254740993", "label": "big"}],
                "value": None,
            },
        )
        self.assertEqual(http_routine_proposal.canonical_question(wire), wire)

    def test_more_targets_than_a_question_shows_offer_none(self) -> None:
        items = {"items": [{"name": "beta", "id": f"id-{index:04d}"} for index in range(9)]}
        calls = (("reports/fetch", {}, items), ("cloudflare/list-dns-records", {"zone_id": "id-0003"}, {}))
        self.assertEqual(
            _record(_send(*calls, message=f"beta\n{EVERY_HOUR}")), recording.Question("routine-binding-ambiguous")
        )
        unshowable = {"items": [{"name": "beta", "id": "x" * 200}, {"name": "beta", "id": "y" * 200}]}
        calls = (("reports/fetch", {}, unshowable), ("cloudflare/list-dns-records", {"zone_id": "x" * 200}, {}))
        self.assertEqual(
            _record(_send(*calls, message=f"beta\n{EVERY_HOUR}")), recording.Question("routine-binding-ambiguous")
        )


if __name__ == "__main__":
    unittest.main()
