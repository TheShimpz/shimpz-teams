"""Team's deterministic recording of a Routine from a person's recent sends, with no model (ADR-0101).

Each top-level input member of a plan call is classified by the first rule that applies: a secret refuses, a value the
person named is a literal, the send's UTC date is the run date, a value one result holds is copied from that one
occurrence, and anything else is a literal the assistant chose; what cannot be read is asked, never guessed.
"""

from __future__ import annotations

import datetime
import unittest
from unittest import mock

from protocol.http.v1 import payload as http_payload
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
    lines = tuple(line for entry in window for line in http_payload.person_lines(entry))
    occurrences = tuple(_occurrence(call, started_at) for call in calls)
    return recording.Send(message, http_payload.person_lines(message), lines, timezone, started_at, occurrences)


def _record(*sends: recording.Send, mode: str = "show", **options) -> recording.Recorded | recording.Question:
    """Record a span; options are ``when``, ``decide``, ``protection``, ``contracts``, ``asked``, ``existing``."""
    return recording.record(
        sends,
        recording.Recording(mode, options.get("when"), options.get("decide", ())),
        options.get("protection") or trace.Protection(),
        options.get("contracts", CONTRACTS),
        asked=options.get("asked"),
        existing=options.get("existing"),
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
                "output": {"mode": "show", "step": "s2", "when": None},
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
        asked = _record(_send(twins, RECORDS, message="DNS de shimpz.com a cada hora"))
        targets = ({"value": SHIMPZ_ID, "label": "shimpz.com"}, {"value": TWIN_ID, "label": "shimpz.com"})
        self.assertEqual(asked, recording.Question("routine-binding-ambiguous", targets))
        pending = recording.Asked(asked.code, (SHIMPZ_ID, TWIN_ID), 1)
        # Naming the zone the work used makes it a literal the person named.
        chosen = _recorded(
            _send(twins, RECORDS, message="DNS de shimpz.com a cada hora"),
            _send(message=SHIMPZ_ID),
            asked=pending,
        )
        self.assertEqual(
            (_input(chosen)["zone_id"], chosen.origins["s2"]["zone_id"]),
            ({"kind": "literal", "value": SHIMPZ_ID}, "request"),
        )
        # Naming the other one asks for the work again, with that zone.
        rerun = _record(
            _send(twins, RECORDS, message="DNS de shimpz.com a cada hora"), _send(message=TWIN_ID), asked=pending
        )
        self.assertEqual(rerun, recording.Question("routine-work-rerun"))

    def test_every_zone_listed_without_a_name_is_read_by_its_position(self) -> None:
        calls = [("cloudflare/list-dns-records", {"zone_id": item["id"]}, {"result": []}) for item in ZONES["result"]]
        recorded = _recorded(_send(ZONES_CALL, *calls, message="Liste os DNS de todas as zonas a cada hora"))
        self.assertEqual(_actions(recorded), ["list-zones", *["list-dns-records"] * 4])
        self.assertEqual(
            [step["input"]["zone_id"]["pointer"] for step in recorded.document["steps"][1:]],
            [f"/result/{index}/id" for index in range(4)],
        )
        self.assertEqual(recorded.document["output"]["step"], "s5")


class ClassificationTests(unittest.TestCase):
    def classify(self, value: object, known: str, *earlier: object, **send) -> tuple[object, str]:
        # Each earlier read is a distinct call, so none is the same source as another.
        calls = [("reports/fetch", {"call": index}, item) for index, item in enumerate(earlier)]
        calls.append(("cloudflare/list-dns-records", {"zone_id": "z", "per_page": value}, {}))
        recorded = _recorded(_send(*calls, message=f"{known}\n{EVERY_HOUR}", **send))
        return _input(recorded)["per_page"], recorded.origins[recorded.document["steps"][-1]["id"]]["per_page"]

    def asked(self, value: object, known: str, *earlier: object) -> object:
        calls = [("reports/fetch", {"call": index}, item) for index, item in enumerate(earlier)]
        calls.append(("cloudflare/list-dns-records", {"zone_id": "z", "per_page": value}, {}))
        return _record(_send(*calls, message=f"{known}\n{EVERY_HOUR}"))

    def test_a_value_named_in_the_request_is_a_literal_even_when_it_could_be_copied(self) -> None:
        source, origin = self.classify("abcdefgh", "use abcdefgh please", {"x": "abcdefgh"})
        self.assertEqual((source, origin), ({"kind": "literal", "value": "abcdefgh"}, "request"))

    def test_a_number_counts_only_as_a_whole_token(self) -> None:
        cases = [
            (30, "A cada 30 segundos", "request"),
            (30, "a cada 30.", "request"),
            (30, "(30)", "request"),
            (30, "300 vezes", "assistant"),
            (30, "a30", "assistant"),
            (30, "30a", "assistant"),
            (30, "30.5", "assistant"),
            (123456, "saldo -123456", "asked"),
            (-123456, "saldo -123456", "request"),
            (6, "1e-6", "assistant"),
            (6, "1e6", "assistant"),
            (1000, "1,000", "assistant"),
            (2.5, "2.5 dias", "request"),
        ]
        for value, known, expected in cases:
            with self.subTest(value=value, known=known):
                if expected == "asked":
                    self.assertEqual(self.asked(value, known).code, "routine-binding-unsourced")
                else:
                    self.assertEqual(self.classify(value, known)[1], expected)

    def test_booleans_null_and_empty_strings_are_never_named_or_copied(self) -> None:
        for value in (True, False, None, "", [], {}):
            with self.subTest(value=value):
                source, origin = self.classify(value, "true false null 1 0", {"x": value})
                self.assertEqual((source, origin), ({"kind": "literal", "value": value}, "assistant"))

    def test_the_send_date_is_the_run_date_only_when_utc_and_local_dates_agree(self) -> None:
        source, origin = self.classify("2026-10-05", "relatório", {"day": "2026-10-05"})
        self.assertEqual((source, origin), ({"kind": "run_clock", "format": "date"}, "clock"))
        # Near local midnight on either side, the date is a fixed literal, never copied from an earlier result.
        early = int(datetime.datetime(2026, 10, 5, 1, tzinfo=datetime.UTC).timestamp())
        late = int(datetime.datetime(2026, 10, 5, 23, 30, tzinfo=datetime.UTC).timestamp())
        for started, timezone in ((early, "America/Sao_Paulo"), (late, "Asia/Tokyo")):
            with self.subTest(timezone=timezone):
                source, origin = self.classify(
                    "2026-10-05", "relatório", {"day": "2026-10-05"}, started_at=started, timezone=timezone
                )
                self.assertEqual((source, origin), ({"kind": "literal", "value": "2026-10-05"}, "assistant"))
        _source, origin = self.classify("2026-10-05", "desde 2026-10-05", {"day": "2026-10-05"})
        self.assertEqual(origin, "request")
        source, _origin = self.classify("2026-10-04", "relatório", {"day": "2026-10-04"})
        self.assertEqual(source["kind"], "step_output")

    def test_a_run_date_needs_a_known_timezone(self) -> None:
        calls = [("cloudflare/list-dns-records", {"zone_id": "z", "per_page": "2026-10-05"}, {})]
        asked = _record(_send(*calls, timezone=None))
        self.assertEqual(asked, recording.Question("routine-timezone-unstated"))

    def test_a_value_at_one_position_is_copied_by_pointer(self) -> None:
        source, origin = self.classify("zone-123", "x", {"zone": {"a/b": "zone-123"}})
        self.assertEqual((source, origin), ({"kind": "step_output", "step": "s1", "pointer": "/zone/a~1b"}, "step"))
        source, _origin = self.classify({"k": 1}, "x", {"cfg": {"k": 1}})
        self.assertEqual(source, {"kind": "step_output", "step": "s1", "pointer": "/cfg"})
        source, _origin = self.classify(1234567, "x", {"n": 1234567.0})
        self.assertEqual(source["kind"], "step_output")
        source, _origin = self.classify([1], "x", [1])
        self.assertEqual(source, {"kind": "step_output", "step": "s1", "pointer": ""})
        # Through two arrays, an item is read by its exact indices.
        source, _origin = self.classify("abcdefgh", "known", {"a": [{"b": [{"id": "abcdefgh"}]}]})
        self.assertEqual(source["pointer"], "/a/0/b/0/id")

    def test_short_values_are_the_assistants_and_a_value_nothing_holds_is_asked_about(self) -> None:
        for value, earlier in (("abcde", {"x": "abcde"}), (12345, {"x": 12345}), (-12345, {"x": -12345})):
            with self.subTest(value=value):
                self.assertEqual(
                    self.classify(value, "known", earlier), ({"kind": "literal", "value": value}, "assistant")
                )
        self.assertEqual(self.asked("abcdefgh", "known", {"x": "other"}).code, "routine-binding-unsourced")

    def test_a_value_several_sources_or_positions_hold_is_asked_about(self) -> None:
        same = recording.Question("routine-binding-ambiguous", ({"value": "abcdefgh", "label": None},))
        self.assertEqual(self.asked("abcdefgh", "known", {"x": "abcdefgh", "y": "abcdefgh"}), same)
        self.assertEqual(self.asked("abcdefgh", "known", {"x": "abcdefgh"}, {"y": "abcdefgh"}), same)
        # A container is never chosen: it refuses.
        container = {"cfg": {"k": 1}}
        code = _code(self, lambda: self.asked({"k": 1}, "x", container, container))
        self.assertEqual(code, "routine-recording-ambiguous")

    def test_one_array_reads_by_a_named_unique_member_asks_on_a_shared_one_and_else_reads_by_index(self) -> None:
        items = {"items": [{"name": "alpha", "id": "id-0001"}, {"name": "beta", "id": "id-0002", "rank": 70}]}
        source, origin = self.classify("id-0002", "use beta", items)
        self.assertEqual(
            source, {"kind": "step_output", "step": "s1", "pointer": "/items", "where": {"name": "beta"}, "item": "/id"}
        )
        self.assertEqual(origin, "selector")
        source, _origin = self.classify("id-0002", "rank 70", items)
        self.assertEqual(source["where"], {"rank": 70})
        indexed = [
            ("id-0002", "nothing named", items),
            ("id-0002", "b", items),  # a one-character name never selects
            ("id-0002", "rank -70", items),  # not a whole token
            ("id-0002", "x", {"items": ["id-0002"]}),  # item is not an object
            ("id-0002", "true", {"items": [{"flag": True, "id": "id-0002"}]}),  # booleans never select
            ("id-0002", "2.5", {"items": [{"score": 2.5, "id": "id-0002"}]}),  # floats never select
        ]
        for value, known, earlier in indexed:
            with self.subTest(known=known, earlier=earlier):
                source, origin = self.classify(value, known, earlier)
                position = len(earlier["items"]) - 1
                pointer = f"/items/{position}" + ("" if isinstance(earlier["items"][0], str) else "/id")
                self.assertEqual((source, origin), ({"kind": "step_output", "step": "s1", "pointer": pointer}, "step"))
        shared = {"items": [{"name": "beta", "id": "id-0001"}, {"name": "beta", "id": "id-0002"}]}
        targets = ({"value": "id-0001", "label": "beta"}, {"value": "id-0002", "label": "beta"})
        self.assertEqual(
            self.asked("id-0002", "beta", shared), recording.Question("routine-binding-ambiguous", targets)
        )
        # Two named members, each unique: nothing separates them, so the person is asked.
        self.assertEqual(self.asked("id-0002", "beta rank 70", items).code, "routine-binding-ambiguous")

    def test_the_selected_member_itself_is_never_its_own_key(self) -> None:
        source, _origin = self.classify(
            {"name": "beta", "id": "id-0002"}, "beta", {"items": [{"name": "alpha"}, {"name": "beta", "id": "id-0002"}]}
        )
        self.assertEqual((source["where"], source["item"]), ({"name": "beta"}, ""))

    def test_a_withheld_sibling_could_equal_the_name_so_it_never_selects(self) -> None:
        withheld_member = trace.Kept(
            {"items": [{"name": None, "id": "id-0001"}, {"name": "beta", "id": "id-0002"}]},
            frozenset({"/items/0/name"}),
        )
        withheld_item = trace.Kept({"items": [None, {"name": "beta", "id": "id-0002"}]}, frozenset({"/items/0"}))
        for kept in (withheld_member, withheld_item):
            with self.subTest(kept=kept):
                calls = (("reports/fetch", {}, kept), ("cloudflare/list-dns-records", {"zone_id": "id-0002"}, {}))
                asked = _record(_send(*calls, message=f"beta\n{EVERY_HOUR}"))
                self.assertEqual(asked.code, "routine-binding-ambiguous")

    def test_a_value_only_a_change_returned_is_asked_about(self) -> None:
        calls = (
            ("reports/post", {}, {"post_id": "post-12345"}),
            ("cloudflare/list-dns-records", {"zone_id": "post-12345"}, {}),
        )
        self.assertEqual(_record(_send(*calls)).code, "routine-binding-unsourced")


def _fields(item: trace.Occurrence) -> dict[str, object]:
    return {name: getattr(item, name) for name, spec in item.__dataclass_fields__.items() if spec.init}


class SourceTests(unittest.TestCase):
    def test_identical_reads_with_no_change_between_are_one_source_and_replay_once(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, ZONES_CALL, RECORDS, message=f"shimpz.com\n{EVERY_HOUR}"))
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])

    def test_reads_of_different_results_stay_distinct_sources(self) -> None:
        old = ("reports/fetch", {}, {"id": "old-id-123"})
        new = ("reports/fetch", {}, {"id": "new-id-456"})
        uses = (("reports/post", {"a": "old-id-123"}, {}), ("reports/post", {"b": "new-id-456"}, {}))
        recorded = _recorded(_send(old, new, *uses))
        steps = recorded.document["steps"]
        self.assertEqual(_actions(recorded), ["fetch", "fetch", "post", "post"])
        self.assertEqual((steps[2]["input"]["a"]["step"], steps[3]["input"]["b"]["step"]), ("s1", "s2"))

    def test_reads_a_change_separates_are_never_one_source(self) -> None:
        # Each read holds the zone, and a change may have changed what the first one read: the person is asked.
        asked = _record(
            _send(ZONES_CALL, ("reports/post", {}, {}), ZONES_CALL, RECORDS, message="shimpz.com\na cada hora")
        )
        self.assertEqual(asked, recording.Question("routine-binding-ambiguous", ({"value": SHIMPZ_ID, "label": None},)))

    def test_an_order_that_would_move_a_read_across_a_change_refuses(self) -> None:
        conflict = (("reports/post", {"name": "renamed-1"}, {}), ("reports/fetch", {}, {"name": "renamed-1"}))
        self.assertEqual(_code(self, lambda: _record(_send(*conflict))), "routine-recording-conflict")
        cyclic = (
            ("reports/fetch", {"x": "value-one"}, {"y": "value-two"}),
            ("reports/fetch", {"y": "value-two"}, {"x": "value-one"}),
        )
        self.assertEqual(_code(self, lambda: _record(_send(*cyclic))), "routine-recording-cyclic")

    def test_work_split_across_sends_is_asked_about_until_it_runs_whole(self) -> None:
        first = ("cloudflare/list-dns-records", {"zone_id": ZONES["result"][0]["id"]}, {})
        second = ("cloudflare/list-dns-records", {"zone_id": ZONES["result"][1]["id"]}, {})
        split = _record(_send(ZONES_CALL, first), _send(second))
        self.assertEqual(split, recording.Question("routine-work-split"))
        whole = _recorded(_send(ZONES_CALL, first), _send(first, second))
        self.assertEqual(_actions(whole), ["list-zones", "list-dns-records", "list-dns-records"])

    def test_an_earlier_identical_call_the_work_ran_again_is_no_split(self) -> None:
        earlier = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {"result": [{"type": "A"}]})
        again = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {"result": [{"type": "MX"}]})
        recorded = _recorded(_send(ZONES_CALL, earlier, message="shimpz.com"), _send(ZONES_CALL, again))
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])

    def test_a_named_member_separates_one_position_from_the_others(self) -> None:
        result = {"items": [{"name": "beta", "id": "id-0002"}], "default": "id-0002"}
        calls = (("reports/fetch", {}, result), ("cloudflare/list-dns-records", {"zone_id": "id-0002"}, {}))
        recorded = _recorded(_send(*calls, message=f"beta\n{EVERY_HOUR}"))
        self.assertEqual(_input(recorded)["zone_id"]["where"], {"name": "beta"})

    def test_a_target_that_is_not_a_scalar_is_never_offered(self) -> None:
        result = {"items": [{"name": "beta", "id": True}, {"name": "beta"}, {"name": "beta", "id": "id-0002"}]}
        calls = (("reports/fetch", {}, result), ("cloudflare/list-dns-records", {"zone_id": "id-0002"}, {}))
        asked = _record(_send(*calls, message=f"beta\n{EVERY_HOUR}"))
        self.assertEqual(asked.options, ({"value": "id-0002", "label": "beta"},))

    def test_an_unverifiable_reference_refuses(self) -> None:
        with mock.patch.object(routine_plan, "select", side_effect=routine_plan.PlanError("plan-reference-missing")):
            code = _code(
                self,
                lambda: _record(
                    _send(("reports/fetch", {}, {"id": "zone-1234"}), ("reports/post", {"z": "zone-1234"}, {}))
                ),
            )
        self.assertEqual(code, "routine-recording-unverified")
        with mock.patch.object(recording, "_resolved", return_value="other"):
            code = _code(self, lambda: _record(_send(ZONES_CALL, RECORDS, message=OWNER)))
        self.assertEqual(code, "routine-recording-unverified")

    def test_a_call_whose_input_differs_from_its_step_is_unverified(self) -> None:
        # Were two reads of different input one source, the later one's input would not be the step's.
        twins = _send(("reports/fetch", {}, {"a": 1}), ("reports/fetch", {"extra": 1}, {"a": 1}))
        with mock.patch.object(recording, "_identity", return_value=b"same"):
            self.assertEqual(_code(self, lambda: _record(twins)), "routine-recording-unverified")


class SecretTests(unittest.TestCase):
    def test_secret_values_and_destinations_refuse_the_recording(self) -> None:
        credential = "sk-" + "A1b2C3d4" * 6
        withheld = trace.Kept({"zone_id": None}, frozenset({"/zone_id"}))
        cases = [
            ("cloudflare/list-dns-records", {"zone_id": credential}),
            ("cloudflare/delete-dns-record", {"zone_id": "z", "api_token": "plain"}),
            ("cloudflare/list-dns-records", {"zone_id": "z"}, withheld),
            ("cloudflare/list-dns-records", {"zone_id": "z"}, trace.Kept(None, frozenset({""}))),
        ]
        for call in cases:
            with self.subTest(call=call):
                code = _code(self, lambda call=call: _record(_send((*call[:2], {}, *call[2:]))))
                self.assertEqual(code, "routine-secret-literal")

    def test_a_value_the_span_protects_never_enters_the_plan(self) -> None:
        protection = trace.Protection().grow(("tok-secret-1",))
        call = ("cloudflare/list-dns-records", {"zone_id": "a tok-secret-1"}, {})
        self.assertEqual(_code(self, lambda: _record(_send(call), protection=protection)), "routine-secret-literal")

    def test_a_protected_value_in_any_reference_path_refuses(self) -> None:
        cases = [
            ({"protected-key": {"id": "safe-id123"}}, "x", "protected-key"),
            ({"protected-list": [{"name": "beta", "id": "safe-id123"}]}, "beta", "protected-list"),
            ({"items": [{"name": "beta", "protected-item": "safe-id123"}]}, "beta", "protected-item"),
            ({"items": [{"name": "shimpz.com", "id": "safe-id123"}]}, "shimpz.com", "shimpz.com"),
        ]
        for result, known, protected in cases:
            with self.subTest(protected=protected):
                calls = (("reports/fetch", {}, result), ("cloudflare/list-dns-records", {"zone_id": "safe-id123"}, {}))
                send = _send(*calls, message=f"{known}\n{EVERY_HOUR}")
                protection = trace.Protection().grow((protected,))
                self.assertEqual(
                    _code(self, lambda s=send, p=protection: _record(s, protection=p)), "routine-secret-literal"
                )
                self.assertEqual(_input(_recorded(send))["zone_id"]["kind"], "step_output")

    def test_a_path_longer_than_a_plan_pointer_refuses(self) -> None:
        long_key = "k" * routine_plan.MAX_POINTER
        calls = (
            ("reports/fetch", {}, {long_key: {"id": "safe-id123"}}),
            ("cloudflare/list-dns-records", {"zone_id": "safe-id123"}, {}),
        )
        self.assertEqual(_code(self, lambda: _record(_send(*calls))), "routine-recording-too-large")

    def test_lost_protection_makes_recording_unavailable(self) -> None:
        lost = trace.Protection(lost=True)
        self.assertEqual(
            _code(self, lambda: _record(_send(ZONES_CALL), protection=lost)), "routine-recording-unavailable"
        )

    def test_an_oversize_input_is_too_large(self) -> None:
        oversize = trace.Kept(None, frozenset({""}), oversize=True)
        self.assertEqual(
            _code(self, lambda: _record(_send(("reports/fetch", {}, {}, oversize)))), "routine-recording-too-large"
        )

    def test_the_whole_input_is_checked_against_its_schema_even_when_kept_unwithheld(self) -> None:
        deep: dict[str, object] = {"type": "object"}
        node = deep
        for _ in range(routine_plan.MAX_SECRET_DEPTH + 2):
            node["allOf"] = [{}]
            node = node["allOf"][0]
        unwalkable = {**CONTRACTS, ("reports", "fetch"): routine_plan.ActionContract(PIN, deep, read_only=True)}
        for kept, contracts in (
            (trace.Kept({"api_token": "plain"}), CONTRACTS),
            (trace.Kept({"q": "plain"}), unwalkable),
        ):
            with self.subTest(kept=kept):
                send = _send(("reports/fetch", {}, {}, kept))
                self.assertEqual(
                    _code(self, lambda s=send, c=contracts: _record(s, contracts=c)), "routine-secret-literal"
                )


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

    def test_no_schedule_two_in_one_text_or_one_only_asked_are_asked_again(self) -> None:
        for message in ("Cria uma rotina", "a cada hora e todo dia às 9h", "Todo dia às 9h?"):
            with self.subTest(message=message):
                self.assertEqual(
                    _record(_send(ZONES_CALL, message=message)), recording.Question("routine-schedule-unstated")
                )

    def test_a_replacement_keeps_its_schedule_unless_one_is_stated(self) -> None:
        existing = recording.Existing({"steps": []}, {"kind": "daily", "time": "08:00"}, "Europe/Lisbon", "person")
        kept = _recorded(_send(ZONES_CALL, message="sem mudar o horário"), existing=existing)
        self.assertEqual(
            (kept.schedule, kept.timezone, kept.timezone_source),
            ({"kind": "daily", "time": "08:00"}, "Europe/Lisbon", "person"),
        )
        changed = _recorded(_send(ZONES_CALL, message="a cada hora"), existing=existing)
        self.assertEqual(changed.schedule, {"kind": "hourly", "every": 1})


class TimezoneTests(unittest.TestCase):
    def test_a_written_zone_wins_over_the_browser_and_the_latest_one_counts(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="todo dia às 9h, Europe/Paris"), _send(message="Europe/Lisbon"))
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("Europe/Lisbon", "person"))
        self.assertEqual(recorded.document["timezone"], "Europe/Lisbon")

    def test_two_zones_in_one_send_are_asked_and_a_later_answer_settles_them(self) -> None:
        ambiguous = _send(ZONES_CALL, message="todo dia às 9h, Europe/Paris ou Europe/London")
        self.assertEqual(_record(ambiguous), recording.Question("routine-timezone-ambiguous"))
        answered = _recorded(ambiguous, _send(message="Europe/London"))
        self.assertEqual(answered.timezone, "Europe/London")

    def test_the_latest_browser_zone_counts(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="todo dia às 9h", timezone="Asia/Tokyo"), _send(message="ok"))
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("America/Sao_Paulo", "browser"))

    def test_no_zone_is_a_convention_only_where_none_is_needed(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, message="a cada hora", timezone=None))
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("UTC", "none"))
        daily = _record(_send(ZONES_CALL, message="todo dia às 9h", timezone=None))
        self.assertEqual(daily, recording.Question("routine-timezone-unstated"))
        unzoned = recording.Existing({"steps": []}, {"kind": "hourly", "every": 1}, "UTC", "none")
        self.assertEqual(_recorded(_send(ZONES_CALL, timezone="Asia/Tokyo"), existing=unzoned).timezone, "Asia/Tokyo")


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
        self.assertEqual(recorded.document["output"], {"mode": "changes", "step": "s5", "when": None})

    def test_none_mode_shows_nothing(self) -> None:
        recorded = _recorded(_send(("reports/fetch", {}, {})), mode="none")
        self.assertEqual(recorded.document["output"], {"mode": "none", "step": None, "when": None})

    def test_decide_replays_only_reads_and_permits_every_effect(self) -> None:
        recorded = _recorded(
            _send(
                ("reports/fetch", {}, {"unused": 1}),
                ZONES_CALL,
                ("cloudflare/delete-dns-record", {"zone_id": SHIMPZ_ID, "record_id": "r1"}, {}),
                message=f"shimpz.com\n{EVERY_HOUR}",
            ),
            mode="decide",
            when="changes",
            decide=(("cloudflare", "renew-certificate"), ("cloudflare", "renew-certificate")),
        )
        self.assertEqual(_actions(recorded), ["fetch", "list-zones"])
        self.assertEqual(recorded.document["output"], {"mode": "decide", "step": None, "when": "changes"})
        self.assertEqual(
            [(item["action"], item["read_only"]) for item in recorded.permitted],
            [("delete-dns-record", False), ("list-zones", True), ("renew-certificate", False), ("fetch", True)],
        )

    def test_a_decide_recording_may_replay_nothing(self) -> None:
        recorded = _recorded(_send(), mode="decide", when="always")
        self.assertEqual((recorded.document["steps"], recorded.permitted), ([], ()))

    def test_a_non_decide_recording_needs_a_call(self) -> None:
        self.assertEqual(_code(self, lambda: _record(_send())), "routine-recording-empty")

    def test_decide_actions_and_when_are_closed(self) -> None:
        send = _send(("reports/fetch", {}, {}))
        many = tuple(("cloudflare", f"action-{index}") for index in range(recording.MAX_DECIDE_ACTIONS + 1))
        cases = [
            ({"mode": "show", "decide": (("reports", "post"),)}, "routine-decide-action-invalid"),
            ({"mode": "decide", "when": "always", "decide": (("reports", "absent"),)}, "routine-decide-action-invalid"),
            ({"mode": "decide", "when": "always", "decide": many}, "routine-decide-action-invalid"),
            ({"mode": "decide", "when": None}, "routine-recording-invalid"),
            ({"mode": "show", "when": "always"}, "routine-recording-invalid"),
            ({"mode": "chain"}, "routine-recording-invalid"),
        ]
        for options, code in cases:
            with self.subTest(options=options):
                self.assertEqual(_code(self, lambda o=options: _record(send, **o)), code)

    def test_a_drifted_or_unknown_contract_fails_closed(self) -> None:
        drifted = {
            **CONTRACTS,
            ("cloudflare", "delete-dns-record"): routine_plan.ActionContract(DRIFTED_PIN, DELETE_IN),
        }
        send = _send(("reports/fetch", {}, {}), ("cloudflare/delete-dns-record", {"zone_id": "z"}, {}))
        self.assertEqual(
            _code(self, lambda: _record(send, mode="decide", when="always", contracts=drifted)), "plan-pin-drift"
        )
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
    "output": {"mode": "show", "step": "s2", "when": None},
}


class KeptTests(unittest.TestCase):
    """A replacement that ran no Action keeps the replaced plan's steps exactly (ADR-0101)."""

    def keep(self, mode: str = "changes", **options) -> recording.Recorded | recording.Question:
        existing = recording.Existing(
            options.get("plan", KEPT_PLAN), {"kind": "daily", "time": "08:00"}, "America/Sao_Paulo", "browser"
        )
        return _record(
            _send(message=options.get("message", "now with 50 per page")),
            mode=mode,
            when=options.get("when"),
            protection=options.get("protection"),
            existing=existing,
        )

    def test_the_steps_stay_and_only_how_and_when_they_run_change(self) -> None:
        kept = self.keep()
        self.assertEqual(kept.document["steps"], KEPT_PLAN["steps"])
        self.assertEqual(
            (kept.document["timezone"], kept.schedule), ("America/Sao_Paulo", {"kind": "daily", "time": "08:00"})
        )
        self.assertEqual(kept.document["output"], {"mode": "changes", "step": "s2", "when": None})
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
        decided = self.keep("decide", when="always", plan={**KEPT_PLAN, "steps": []})
        self.assertEqual((decided.document["steps"], decided.document["output"]["step"]), ([], None))
        self.assertEqual(self.keep(message="a cada hora").schedule, {"kind": "hourly", "every": 1})

    def test_a_lost_protection_a_drifted_pin_or_no_step_to_show_refuses(self) -> None:
        drifted = {**KEPT_PLAN, "steps": [{**KEPT_PLAN["steps"][0], "pin": DRIFTED_PIN}]}
        cases = [
            ({"protection": trace.Protection(lost=True)}, "routine-recording-unavailable"),
            ({"plan": drifted}, "plan-pin-drift"),
            ({"plan": {**KEPT_PLAN, "steps": []}}, "routine-recording-empty"),
        ]
        for options, code in cases:
            with self.subTest(code=code):
                self.assertEqual(_code(self, lambda o=options: self.keep(**o)), code)


class QuestionTests(unittest.TestCase):
    def test_a_question_renders_its_closed_wire_form(self) -> None:
        question = recording.Question("routine-binding-ambiguous", ({"value": "a", "label": None},))
        self.assertEqual(
            question.wire(),
            {"code": "routine-binding-ambiguous", "options": [{"value": "a", "label": None}], "value": None},
        )

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
