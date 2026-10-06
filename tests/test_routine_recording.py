"""Team's deterministic recording of a Routine from a chat turn's own trace, with no model (ADR-0101).

Each top-level input member's whole value of each replay step is classified by the first rule that applies: a secret
refuses, a value named in the request is a literal, the pinned turn date is the run date, a value an earlier read-only
step returned at exactly one position is copied (through one array by its single named sibling), and anything else is
a literal the assistant chose.
"""

from __future__ import annotations

import datetime
import unittest

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
EFFECT_PIN = {False: OTHER_PIN, True: PIN}
STARTED = int(datetime.datetime(2026, 10, 5, 15, tzinfo=datetime.UTC).timestamp())
SHIMPZ_ID = "023e105f4ecef8ad9ca31a8372d0c353"
ZONES = {
    "result": [
        {"id": "9a7806061c88ada191ed06f989cc3dac", "name": "example.com", "created_on": "2026-01-01"},
        {"id": "1b3f0c5e2a9d47e8b6c1d0f2a3b4c5d6", "name": "other.org", "created_on": "2026-01-02"},
        {"id": "5d41402abc4b2a76b9719d911017c592", "name": "blog.dev", "created_on": "2026-01-03"},
        {"id": SHIMPZ_ID, "name": "shimpz.com", "created_on": "2026-01-04"},
    ]
}
OWNER_REQUEST = (
    "Cria uma rotina pra mim\n"
    "Pergunta: O que a rotina deve fazer?\nResposta: Listar registros DNS\n"
    "Pergunta: Com que frequência?\nResposta: A cada 30 segundos\n"
    "Pergunta: De qual zona?\nResposta: shimpz.com"
)


def _trace(*calls: tuple, turn_date: str | None = "2026-10-05", started_at: int = STARTED) -> trace.Trace:
    """A trace of successful calls: (assistant action, input, result[, kept input])."""
    recorded = trace.Trace(turn_date, started_at)
    for call in calls:
        name, given, result = call[:3]
        assistant, action = name.split("/")
        contract = CONTRACTS[(assistant, action)]
        kept_input = call[3] if len(call) > 3 else trace.keep(given, contract.input_schema, ())
        output_schema = ZONES_OUT if action == "list-zones" else {}
        recorded = recorded.add(
            trace.Occurrence(
                operation_id=f"6f1c2b8e-3a4d-4c5e-9f60-{len(recorded.occurrences):012d}",
                assistant=assistant,
                action=action,
                pin=contract.pin,
                read_only=contract.read_only,
                dispatched_at=started_at,
                input=kept_input,
                result=trace.keep(result, output_schema, ()),
            )
        )
    return recorded


def _record(recorded: trace.Trace, known: str, mode: str = "show", **options) -> recording.Recorded:
    """Record a trace; options are ``when``, ``timezone``, ``decide``, ``protection``, and ``contracts``."""
    return recording.record(
        recorded,
        recording.Recording(
            mode, options.get("when"), options.get("timezone", "America/Sao_Paulo"), options.get("decide", ())
        ),
        known,
        options.get("protection") or trace.Protection(),
        options.get("contracts", CONTRACTS),
    )


def _code(test: unittest.TestCase, call) -> str:
    with test.assertRaises((recording.RecordingError, routine_plan.PlanError)) as raised:
        call()
    return raised.exception.code


def _input(recorded: recording.Recorded, step: int = -1) -> dict[str, object]:
    return recorded.document["steps"][step]["input"]


class OwnerCaseTests(unittest.TestCase):
    def test_the_zone_id_is_selected_by_the_zone_name_the_person_gave(self) -> None:
        recorded = _record(
            _trace(
                ("cloudflare/list-zones", {}, ZONES),
                ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {"result": [{"type": "A"}]}),
            ),
            OWNER_REQUEST,
        )
        self.assertEqual(
            recorded.document,
            {
                "version": routine_plan.RECORDED_VERSION,
                "timezone": "America/Sao_Paulo",
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
                                "where": {"name": "shimpz.com"},
                                "item": "/id",
                            }
                        },
                    },
                ],
                "output": {"mode": "show", "step": "s2", "when": None},
            },
        )
        self.assertEqual(recorded.origins, {"s1": {}, "s2": {"zone_id": "selector"}})
        self.assertEqual(
            recorded.permitted,
            (
                {"assistant": "cloudflare", "action": "list-dns-records", "pin": PIN, "read_only": True},
                {"assistant": "cloudflare", "action": "list-zones", "pin": PIN, "read_only": True},
            ),
        )
        selected = routine_plan.select_where(ZONES, "/result", {"name": "shimpz.com"}, "/id")
        self.assertEqual(selected, SHIMPZ_ID)


class ClassificationTests(unittest.TestCase):
    def classify(self, value: object, known: str, *earlier: object, **options) -> tuple[object, str]:
        calls = [("reports/fetch", {}, item) for item in earlier]
        calls.append(("cloudflare/list-dns-records", {"zone_id": "z", "per_page": value}, {}))
        recorded = _record(_trace(*calls, **options.pop("trace", {})), known, **options)
        return _input(recorded)["per_page"], recorded.origins[recorded.document["steps"][-1]["id"]]["per_page"]

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
            (123456, "saldo -123456", "assistant"),
            (-123456, "saldo -123456", "request"),
            (123456, "x+123456", "assistant"),
            (123456, "zona-123456", "assistant"),
            (6, "1e-6", "assistant"),
            (6, "1e+6", "assistant"),
            (6, "1e6", "assistant"),
            (1000, "1,000", "assistant"),
            (1000, "1.000", "assistant"),
            (2.5, "2.5 dias", "request"),
        ]
        for value, known, expected in cases:
            with self.subTest(value=value, known=known):
                self.assertEqual(self.classify(value, known)[1], expected)

    def test_booleans_null_and_empty_strings_are_never_named_or_copied(self) -> None:
        for value in (True, False, None, "", [], {}):
            with self.subTest(value=value):
                source, origin = self.classify(value, "true false null 1 0", {"x": value})
                self.assertEqual((source, origin), ({"kind": "literal", "value": value}, "assistant"))

    def test_the_turn_date_is_the_run_date_only_when_utc_and_local_dates_agree(self) -> None:
        source, origin = self.classify("2026-10-05", "relatório", {"day": "2026-10-05"})
        self.assertEqual((source, origin), ({"kind": "run_clock", "format": "date"}, "clock"))
        # Near local midnight on either side, the date is a fixed literal, never copied from an earlier result.
        early = int(datetime.datetime(2026, 10, 5, 1, tzinfo=datetime.UTC).timestamp())
        late = int(datetime.datetime(2026, 10, 5, 23, 30, tzinfo=datetime.UTC).timestamp())
        for started, timezone in ((early, "America/Sao_Paulo"), (late, "Asia/Tokyo")):
            with self.subTest(timezone=timezone):
                source, origin = self.classify(
                    "2026-10-05", "relatório", {"day": "2026-10-05"}, trace={"started_at": started}, timezone=timezone
                )
                self.assertEqual((source, origin), ({"kind": "literal", "value": "2026-10-05"}, "assistant"))
        source, origin = self.classify("2026-10-05", "desde 2026-10-05", {"day": "2026-10-05"})
        self.assertEqual(origin, "request")
        source, origin = self.classify("2026-10-04", "relatório", {"day": "2026-10-04"})
        self.assertEqual(source["kind"], "step_output")
        source, origin = self.classify("2026-10-05", "relatório", trace={"turn_date": None})
        self.assertEqual(origin, "assistant")

    def test_a_value_at_one_position_outside_arrays_is_copied_by_pointer(self) -> None:
        source, origin = self.classify("zone-123", "x", {"zone": {"a/b": "zone-123"}})
        self.assertEqual(source, {"kind": "step_output", "step": "s1", "pointer": "/zone/a~1b"})
        self.assertEqual(origin, "step")
        source, _origin = self.classify({"k": 1}, "x", {"cfg": {"k": 1}})
        self.assertEqual(source, {"kind": "step_output", "step": "s1", "pointer": "/cfg"})
        source, _origin = self.classify(1234567, "x", {"n": 1234567})
        self.assertEqual(source["kind"], "step_output")
        source, _origin = self.classify(1234567, "x", {"n": 1234567.0})
        self.assertEqual(source["kind"], "step_output")
        source, _origin = self.classify([1], "x", [1])
        self.assertEqual(source, {"kind": "step_output", "step": "s1", "pointer": ""})

    def test_short_values_ambiguous_positions_and_two_arrays_are_fixed_literals(self) -> None:
        cases = [
            ("abcde", ({"x": "abcde"},)),
            (12345, ({"x": 12345},)),
            (-12345, ({"x": -12345},)),
            ("abcdefgh", ({"x": "abcdefgh", "y": "abcdefgh"},)),
            ("abcdefgh", ({"x": "abcdefgh"}, {"y": "abcdefgh"})),
            ("abcdefgh", ({"a": [{"b": [{"name": "known", "id": "abcdefgh"}], "name": "known"}]},)),
            ("abcdefgh", ({"x": "other"},)),
        ]
        for value, earlier in cases:
            with self.subTest(value=value, earlier=earlier):
                source, origin = self.classify(value, "known", *earlier)
                self.assertEqual((source, origin), ({"kind": "literal", "value": value}, "assistant"))

    def test_one_array_needs_exactly_one_named_unique_sibling(self) -> None:
        items = {"items": [{"name": "alpha", "id": "id-0001"}, {"name": "beta", "id": "id-0002", "rank": 70}]}
        source, origin = self.classify("id-0002", "use beta", items)
        self.assertEqual(
            source, {"kind": "step_output", "step": "s1", "pointer": "/items", "where": {"name": "beta"}, "item": "/id"}
        )
        self.assertEqual(origin, "selector")
        source, _origin = self.classify("id-0002", "rank 70", items)
        self.assertEqual(source["where"], {"rank": 70})
        fixed = [
            ("id-0002", "beta rank 70", items),  # two candidates
            ("id-0002", "nothing named", items),  # no candidate
            ("id-0002", "b", items),  # a one-character name never selects
            ("id-0002", "rank -70", items),  # not a whole token
            ("id-0002", "beta", {"items": [{"name": "beta", "id": "id-0001"}, {"name": "beta", "id": "id-0002"}]}),
            ("id-0002", "x", {"items": ["id-0002"]}),  # item is not an object
            ("id-0002", "true", {"items": [{"flag": True, "id": "id-0002"}]}),  # booleans never select
            ("id-0002", "2.5", {"items": [{"score": 2.5, "id": "id-0002"}]}),  # floats never select
        ]
        for value, known, earlier in fixed:
            with self.subTest(known=known, earlier=earlier):
                source, origin = self.classify(value, known, earlier)
                self.assertEqual((source, origin), ({"kind": "literal", "value": value}, "assistant"))

    def test_the_selected_member_itself_is_never_its_own_key(self) -> None:
        source, _origin = self.classify(
            {"name": "beta", "id": "id-0002"}, "beta", {"items": [{"name": "alpha"}, {"name": "beta", "id": "id-0002"}]}
        )
        self.assertEqual(source["where"], {"name": "beta"})
        self.assertEqual(source["item"], "")
        source, origin = self.classify("beta-long", "x", {"items": [{"name": "beta-long"}]})
        self.assertEqual((source, origin), ({"kind": "literal", "value": "beta-long"}, "assistant"))

    def test_a_withheld_sibling_could_equal_the_name_so_it_never_selects(self) -> None:
        withheld_member = trace.Kept(
            {"items": [{"name": None, "id": "id-0001"}, {"name": "beta", "id": "id-0002"}]},
            frozenset({"/items/0/name"}),
        )
        withheld_item = trace.Kept({"items": [None, {"name": "beta", "id": "id-0002"}]}, frozenset({"/items/0"}))
        for kept in (withheld_member, withheld_item):
            with self.subTest(kept=kept):
                recorded = _trace(
                    ("reports/fetch", {}, {}),
                    ("cloudflare/list-dns-records", {"zone_id": "id-0002"}, {}),
                )
                fetched = recorded.occurrences[0]
                patched = trace.Trace(
                    recorded.turn_date,
                    recorded.started_at,
                    (trace.Occurrence(**{**_fields(fetched), "result": kept}), recorded.occurrences[1]),
                )
                result = _record(patched, "beta")
                self.assertEqual(_input(result)["zone_id"], {"kind": "literal", "value": "id-0002"})

    def test_a_value_copied_only_from_an_effect_is_a_fixed_literal(self) -> None:
        recorded = _record(
            _trace(
                ("reports/post", {}, {"post_id": "post-12345"}),
                ("cloudflare/list-dns-records", {"zone_id": "post-12345"}, {}),
            ),
            "x",
        )
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "literal", "value": "post-12345"})
        recorded = _record(
            _trace(
                ("reports/fetch", {}, {"id": "post-12345"}),
                ("reports/post", {}, {"post_id": "post-12345"}),
                ("cloudflare/list-dns-records", {"zone_id": "post-12345"}, {}),
            ),
            "x",
        )
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "literal", "value": "post-12345"})


def _fields(item: trace.Occurrence) -> dict[str, object]:
    return {name: getattr(item, name) for name, spec in item.__dataclass_fields__.items() if spec.init}


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
                code = _code(self, lambda call=call: _record(_trace((*call[:2], {}, *call[2:])), "x"))
                self.assertEqual(code, "routine-secret-literal")

    def test_a_value_the_turn_protects_never_enters_the_plan(self) -> None:
        protection = trace.Protection().grow(("tok-secret-1",))
        code = _code(
            self,
            lambda: _record(
                _trace(("cloudflare/list-dns-records", {"zone_id": "a tok-secret-1"}, {})), "x", protection=protection
            ),
        )
        self.assertEqual(code, "routine-secret-literal")

    def test_a_selector_name_protected_later_in_the_turn_refuses(self) -> None:
        calls = (
            ("cloudflare/list-zones", {}, ZONES),
            ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {}),
        )
        protection = trace.Protection().grow(("shimpz.com",))
        self.assertEqual(
            _code(self, lambda: _record(_trace(*calls), OWNER_REQUEST, protection=protection)),
            "routine-secret-literal",
        )
        keyed = {"result": [{"id": "x" * 32, "tok-key": "alpha"}, {"id": SHIMPZ_ID, "tok-key": "beta"}]}
        protection = trace.Protection().grow(("tok-key",))
        recorded = _trace(("reports/fetch", {}, keyed), ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {}))
        self.assertEqual(
            _code(self, lambda: _record(recorded, "beta", protection=protection)), "routine-secret-literal"
        )

    def test_lost_protection_makes_recording_unavailable(self) -> None:
        lost = trace.Protection(lost=True)
        code = _code(self, lambda: _record(_trace(("reports/fetch", {}, {})), "x", protection=lost))
        self.assertEqual(code, "routine-recording-unavailable")

    def test_an_oversize_input_is_too_large(self) -> None:
        oversize = trace.Kept(None, frozenset({""}), oversize=True)
        code = _code(self, lambda: _record(_trace(("reports/fetch", {}, {}, oversize)), "x"))
        self.assertEqual(code, "routine-recording-too-large")


class BoundaryTests(unittest.TestCase):
    def test_every_call_replays_and_unreferenced_middle_reads_are_pruned(self) -> None:
        recorded = _record(
            _trace(
                ("reports/fetch", {}, {"unused": 1}),
                ("cloudflare/list-zones", {}, ZONES),
                ("reports/post", {"text": "hello"}, {"ok": True}),
                ("reports/fetch", {"q": 1}, {}),
                ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {}),
            ),
            "shimpz.com",
            "changes",
        )
        steps = recorded.document["steps"]
        self.assertEqual(
            [(step["id"], step["action"]) for step in steps],
            [("s1", "list-zones"), ("s2", "post"), ("s3", "list-dns-records")],
        )
        self.assertEqual(steps[2]["input"]["zone_id"]["step"], "s1")
        self.assertEqual(recorded.document["output"], {"mode": "changes", "step": "s3", "when": None})
        self.assertEqual(set(recorded.origins), {"s1", "s2", "s3"})
        self.assertEqual(recorded.origins["s2"], {"text": "assistant"})

    def test_none_mode_shows_nothing_and_still_keeps_the_last_step(self) -> None:
        recorded = _record(_trace(("reports/fetch", {}, {}), ("reports/fetch", {}, {})), "x", "none")
        self.assertEqual(len(recorded.document["steps"]), 1)
        self.assertEqual(recorded.document["output"], {"mode": "none", "step": None, "when": None})

    def test_decide_replays_only_reads_and_permits_every_effect(self) -> None:
        recorded = _record(
            _trace(
                ("reports/fetch", {}, {"unused": 1}),
                ("cloudflare/list-zones", {}, ZONES),
                ("cloudflare/delete-dns-record", {"zone_id": SHIMPZ_ID, "record_id": "r1"}, {}),
            ),
            "shimpz.com",
            "decide",
            when="changes",
            decide=(("cloudflare", "renew-certificate"), ("cloudflare", "renew-certificate")),
        )
        self.assertEqual([step["action"] for step in recorded.document["steps"]], ["fetch", "list-zones"])
        self.assertEqual(recorded.document["output"], {"mode": "decide", "step": None, "when": "changes"})
        self.assertEqual(
            [(item["action"], item["read_only"]) for item in recorded.permitted],
            [("delete-dns-record", False), ("list-zones", True), ("renew-certificate", False), ("fetch", True)],
        )

    def test_a_decide_recording_may_replay_nothing(self) -> None:
        recorded = _record(trace.Trace("2026-10-05", STARTED), "x", "decide", when="always")
        self.assertEqual(recorded.document["steps"], [])
        self.assertEqual(recorded.permitted, ())

    def test_a_non_decide_recording_needs_a_call(self) -> None:
        empty = trace.Trace("2026-10-05", STARTED)
        self.assertEqual(_code(self, lambda: _record(empty, "x")), "routine-recording-empty")

    def test_decide_actions_and_when_are_closed(self) -> None:
        recorded = _trace(("reports/fetch", {}, {}))
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
                options = dict(options)
                mode = options.pop("mode")
                self.assertEqual(_code(self, lambda o=options, m=mode: _record(recorded, "x", m, **o)), code)

    def test_a_drifted_or_unknown_contract_fails_closed(self) -> None:
        drifted = {
            **CONTRACTS,
            ("cloudflare", "delete-dns-record"): routine_plan.ActionContract(DRIFTED_PIN, DELETE_IN),
        }
        mutating = _trace(("reports/fetch", {}, {}), ("cloudflare/delete-dns-record", {"zone_id": "z"}, {}))
        code = _code(self, lambda: _record(mutating, "x", "decide", when="always", contracts=drifted))
        self.assertEqual(code, "plan-pin-drift")
        missing = {key: value for key, value in CONTRACTS.items() if key != ("reports", "fetch")}
        self.assertEqual(_code(self, lambda: _record(mutating, "x", contracts=missing)), "plan-pin-drift")


if __name__ == "__main__":
    unittest.main()


class PruningTests(unittest.TestCase):
    def test_a_read_only_step_whose_only_reader_was_pruned_is_pruned_too(self) -> None:
        recorded = _record(
            _trace(
                ("cloudflare/list-zones", {}, ZONES),
                ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {}),
                ("reports/fetch", {}, {"done": True}),
            ),
            "shimpz.com",
        )
        self.assertEqual([step["action"] for step in recorded.document["steps"]], ["fetch"])
        self.assertEqual(recorded.document["output"], {"mode": "show", "step": "s1", "when": None})


class WholeInputSecretTests(unittest.TestCase):
    def test_the_whole_input_is_checked_against_its_schema_even_when_kept_unwithheld(self) -> None:
        deep: dict[str, object] = {"type": "object"}
        node = deep
        for _ in range(routine_plan.MAX_SECRET_DEPTH + 2):
            node["allOf"] = [{}]
            node = node["allOf"][0]
        unwalkable = {**CONTRACTS, ("reports", "fetch"): routine_plan.ActionContract(PIN, deep, read_only=True)}
        cases = [
            (trace.Kept({"api_token": "plain"}), CONTRACTS),
            (trace.Kept({"q": "plain"}), unwalkable),
        ]
        for kept, contracts in cases:
            with self.subTest(kept=kept):
                recorded = _trace(("reports/fetch", {}, {}, kept))
                code = _code(self, lambda r=recorded, c=contracts: _record(r, "x", contracts=c))
                self.assertEqual(code, "routine-secret-literal")


class ReferencePathProtectionTests(unittest.TestCase):
    def test_a_protected_value_in_any_reference_path_refuses(self) -> None:
        cases = [
            ({"protected-key": {"id": "safe-id123"}}, "safe-id123", "x", "protected-key"),
            ({"a/protected": {"id": "safe-id123"}}, "safe-id123", "x", "a/protected"),
            ({"pro": {"tected": {"id": "safe-id123"}}}, "safe-id123", "x", "pro/tected"),
            ({"protected-list": [{"name": "beta", "id": "safe-id123"}]}, "safe-id123", "beta", "protected-list"),
            ({"items": [{"name": "beta", "protected-item": "safe-id123"}]}, "safe-id123", "beta", "protected-item"),
        ]
        for result, value, known, protected in cases:
            with self.subTest(protected=protected):
                recorded = _trace(
                    ("reports/fetch", {}, result), ("cloudflare/list-dns-records", {"zone_id": value}, {})
                )
                protection = trace.Protection().grow((protected,))
                code = _code(self, lambda r=recorded, k=known, p=protection: _record(r, k, protection=p))
                self.assertEqual(code, "routine-secret-literal")
                self.assertEqual(_input(_record(recorded, known))["zone_id"]["kind"], "step_output")


class UnrepresentableReferenceTests(unittest.TestCase):
    def test_a_path_longer_than_a_plan_pointer_falls_to_a_fixed_literal(self) -> None:
        long_key = "k" * routine_plan.MAX_POINTER
        cases = [
            ({long_key: {"id": "safe-id123"}}, "x"),
            ({"items": [{"name": "beta", long_key: "safe-id123"}]}, "beta"),
        ]
        for result, known in cases:
            with self.subTest(known=known):
                recorded = _trace(
                    ("reports/fetch", {}, result), ("cloudflare/list-dns-records", {"zone_id": "safe-id123"}, {})
                )
                self.assertEqual(
                    _input(_record(recorded, known))["zone_id"], {"kind": "literal", "value": "safe-id123"}
                )
