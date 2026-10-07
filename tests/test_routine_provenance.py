"""Each recorded input is a literal, the run date, a copied source, or a question, never a guess (ADR-0101)."""

from __future__ import annotations

import datetime
import unittest
from unittest import mock

from test_routine_recording import (
    CONTRACTS,
    EVERY_HOUR,
    OWNER,
    PIN,
    RECORDS,
    SHIMPZ_ID,
    ZONES,
    ZONES_CALL,
    _actions,
    _code,
    _input,
    _record,
    _recorded,
    _send,
    _then_hourly,
)

from routine import compose as routine_compose
from routine import plan as routine_plan
from routine import recording, trace


class ClassificationTests(unittest.TestCase):
    def classify(self, value: object, known: str, *earlier: object, **send) -> tuple[object, str]:
        # Each earlier read is a distinct call, so none is the same source as another.
        calls = [("reports/fetch", {"call": index}, item) for index, item in enumerate(earlier)]
        calls.append(("cloudflare/list-dns-records", {"zone_id": "z", "per_page": value}, {}))
        recorded = _recorded(_send(*calls, message=_then_hourly(known), **send))
        return _input(recorded)["per_page"], recorded.origins[recorded.document["steps"][-1]["id"]]["per_page"]

    def asked(self, value: object, known: str, *earlier: object) -> object:
        calls = [("reports/fetch", {"call": index}, item) for index, item in enumerate(earlier)]
        calls.append(("cloudflare/list-dns-records", {"zone_id": "z", "per_page": value}, {}))
        return _record(_send(*calls, message=_then_hourly(known)))

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

    def test_the_send_date_in_the_routines_zone_is_the_run_date(self) -> None:
        source, origin = self.classify("2026-10-05", "relatório", {"day": "2026-10-05"})
        self.assertEqual((source, origin), ({"kind": "run_clock", "format": "date"}, "clock"))
        # At 23:30 in São Paulo the person's date is the run date, though it is already the next day in UTC.
        evening = int(datetime.datetime(2026, 10, 7, 2, 30, tzinfo=datetime.UTC).timestamp())
        source, origin = self.classify("2026-10-06", "relatório", started_at=evening)
        self.assertEqual((source, origin), ({"kind": "run_clock", "format": "date"}, "clock"))
        source, origin = self.classify("2026-10-07", "relatório", started_at=evening)
        self.assertEqual((source, origin), ({"kind": "literal", "value": "2026-10-07"}, "assistant"))
        # A zone the person wrote is the Routine's zone: there, at that instant, the UTC date is the run date.
        source, origin = self.classify("2026-10-07", "relatório em UTC", started_at=evening)
        self.assertEqual((source, origin), ({"kind": "run_clock", "format": "date"}, "clock"))
        # The UTC date of a send on another local date is a fixed literal, never copied from an earlier result.
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

    def test_with_no_zone_known_the_run_date_is_the_utc_date(self) -> None:
        calls = [("cloudflare/list-dns-records", {"zone_id": "z", "per_page": "2026-10-05"}, {})]
        recorded = _recorded(_send(*calls, timezone=None))
        self.assertEqual(_input(recorded)["per_page"], {"kind": "run_clock", "format": "date"})
        self.assertEqual((recorded.timezone, recorded.timezone_source), ("UTC", "none"))

    def test_a_value_at_one_position_is_copied_by_pointer(self) -> None:
        source, origin = self.classify("zone-123", "x", {"zone": {"a/b": "zone-123"}})
        self.assertEqual((source, origin), ({"kind": "step_output", "step": "s1", "pointer": "/zone/a~1b"}, "step"))
        source, _origin = self.classify({"k": 1}, "x", {"cfg": {"k": 1}})
        self.assertEqual(source, {"kind": "step_output", "step": "s1", "pointer": "/cfg"})
        source, _origin = self.classify(1234567, "x", {"n": 1234567.0})
        self.assertEqual(source["kind"], "step_output")
        source, _origin = self.classify([1], "x", [1])
        self.assertEqual(source, {"kind": "step_output", "step": "s1", "pointer": ""})

    def test_short_values_are_the_assistants_and_a_value_nothing_holds_is_asked_about(self) -> None:
        for value, earlier in (("abcde", {"x": "abcde"}), (12345, {"x": 12345}), (-12345, {"x": -12345})):
            with self.subTest(value=value):
                self.assertEqual(
                    self.classify(value, "known", earlier), ({"kind": "literal", "value": value}, "assistant")
                )
        self.assertEqual(self.asked("abcdefgh", "known", {"x": "other"}).code, "routine-binding-unsourced")

    def test_a_value_inside_an_array_without_a_named_member_is_asked_about(self) -> None:
        unnamed = {"items": [{"id": "id-0001", "kind": "zone"}, {"id": "id-0002", "kind": "zone"}]}
        targets = ({"value": "id-0001", "label": None}, {"value": "id-0002", "label": None})
        self.assertEqual(
            self.asked("id-0002", "nothing named", unnamed), recording.Question("routine-binding-ambiguous", targets)
        )
        # An item without the consumed member is no target.
        missing = {"items": [{"kind": "zone"}, *unnamed["items"]]}
        self.assertEqual(self.asked("id-0002", "nothing named", missing).options, targets)
        plain = {"items": ["id-0001", "id-0002"]}
        self.assertEqual(self.asked("id-0002", "x", plain), recording.Question("routine-binding-ambiguous", targets))
        # Only one name-like member labels a target.
        labelled = {"items": [{"id": "id-0001", "name": "alpha", "title": "A"}, {"id": "id-0002", "name": "beta"}]}
        found = self.asked("id-0002", "x", labelled).options
        self.assertEqual(found, ({"value": "id-0001", "label": None}, {"value": "id-0002", "label": "beta"}))
        nested = {"a": [{"b": [{"id": "abcdefgh"}]}]}
        self.assertEqual(
            self.asked("abcdefgh", "known", nested),
            recording.Question("routine-binding-ambiguous", ({"value": "abcdefgh", "label": None},)),
        )

    def test_free_text_nothing_holds_is_the_assistants_and_only_an_identifier_is_asked_about(self) -> None:
        for value in ("Resumo diário do DNS", "two words here", {"note": "x"}, ["a", "b"]):
            with self.subTest(value=value):
                self.assertEqual(self.classify(value, "known"), ({"kind": "literal", "value": value}, "assistant"))
        for value in ("zone-without-spaces", 1234567):
            with self.subTest(value=value):
                self.assertEqual(self.asked(value, "known").code, "routine-binding-unsourced")

    def test_the_persons_answer_naming_an_unsourced_value_settles_it(self) -> None:
        work = _send(RECORDS, message="DNS a cada hora")
        self.assertEqual(_record(work), recording.Question("routine-binding-unsourced"))
        pending = recording.Asked("routine-binding-unsourced", 1)
        answered = _recorded(work, _send(message=f"O id é {SHIMPZ_ID}"), asked=pending)
        self.assertEqual(
            (_input(answered)["zone_id"], answered.origins["s1"]["zone_id"]),
            ({"kind": "literal", "value": SHIMPZ_ID}, "request"),
        )

    def test_a_value_several_sources_or_positions_hold_is_asked_about(self) -> None:
        same = recording.Question("routine-binding-ambiguous", ({"value": "abcdefgh", "label": None},))
        self.assertEqual(self.asked("abcdefgh", "known", {"x": "abcdefgh", "y": "abcdefgh"}), same)
        self.assertEqual(self.asked("abcdefgh", "known", {"x": "abcdefgh"}, {"y": "abcdefgh"}), same)
        # A container is never chosen: it refuses.
        container = {"cfg": {"k": 1}}
        code = _code(self, lambda: self.asked({"k": 1}, "x", container, container))
        self.assertEqual(code, "routine-recording-ambiguous")

    def test_one_array_reads_only_by_a_named_unique_member_and_asks_otherwise(self) -> None:
        items = {"items": [{"name": "alpha", "id": "id-0001"}, {"name": "beta", "id": "id-0002", "rank": 70}]}
        source, origin = self.classify("id-0002", "use beta", items)
        self.assertEqual(
            source, {"kind": "step_output", "step": "s1", "pointer": "/items", "where": {"name": "beta"}, "item": "/id"}
        )
        self.assertEqual(origin, "selector")
        source, _origin = self.classify("id-0002", "rank 70", items)
        self.assertEqual(source["where"], {"rank": 70})
        unnamed = [
            ("id-0002", "nothing named", items),
            ("id-0002", "b", items),  # a one-character name never selects
            ("id-0002", "rank -70", items),  # not a whole token
            ("id-0002", "x", {"items": ["id-0002"]}),  # item is not an object
            ("id-0002", "true", {"items": [{"flag": True, "id": "id-0002"}]}),  # booleans never select
            ("id-0002", "2.5", {"items": [{"score": 2.5, "id": "id-0002"}]}),  # floats never select
        ]
        for value, known, earlier in unnamed:
            with self.subTest(known=known, earlier=earlier):
                self.assertEqual(self.asked(value, known, earlier).code, "routine-binding-ambiguous")
        shared = {"items": [{"name": "beta", "id": "id-0001"}, {"name": "beta", "id": "id-0002"}]}
        targets = ({"value": "id-0001", "label": "beta"}, {"value": "id-0002", "label": "beta"})
        self.assertEqual(
            self.asked("id-0002", "beta", shared), recording.Question("routine-binding-ambiguous", targets)
        )
        # Two named members, each unique: the name-like one selects the item, in whatever order the person wrote them.
        for known in ("beta rank 70", "rank 70 beta"):
            with self.subTest(known=known):
                source, _origin = self.classify("id-0002", known, items)
                self.assertEqual(source["where"], {"name": "beta"})
        # Two unique name-like members, or two unique members of which none is name-like: nothing separates them.
        titled = {
            "items": [
                {"name": "alpha", "title": "First", "id": "id-0001"},
                {"name": "beta", "title": "Second", "id": "id-0002"},
            ]
        }
        self.assertEqual(self.asked("id-0002", "beta Second", titled).code, "routine-binding-ambiguous")
        ranked = {"items": [{"rank": 1, "size": 10, "id": "id-0001"}, {"rank": 70, "size": 2000, "id": "id-0002"}]}
        self.assertEqual(self.asked("id-0002", "rank 70 size 2000", ranked).code, "routine-binding-ambiguous")

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

    def test_a_value_a_change_returned_binds_to_it_except_where_changes_do_not_replay(self) -> None:
        calls = (
            ("reports/post", {}, {"post_id": "post-12345"}),
            ("cloudflare/list-dns-records", {"zone_id": "post-12345"}, {}),
        )
        recorded = _recorded(_send(*calls))
        self.assertEqual(_actions(recorded), ["post", "list-dns-records"])
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "step_output", "step": "s1", "pointer": "/post_id"})
        # In an earlier send, the change still replays as the work's source, where it ran.
        earlier = _recorded(_send(calls[0]), _send(calls[1]))
        self.assertEqual(_actions(earlier), ["post", "list-dns-records"])
        # A decision's changes never replay, so nothing may read one: the person is asked.
        decided = _record(_send(*calls), mode="decide", when="always")
        self.assertEqual(decided, recording.Question("routine-binding-unsourced"))


class SourceTests(unittest.TestCase):
    def test_identical_reads_with_no_change_between_are_one_source_and_replay_once(self) -> None:
        recorded = _recorded(_send(ZONES_CALL, ZONES_CALL, RECORDS, message=f"shimpz.com\n{EVERY_HOUR}"))
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])

    def test_reads_equal_as_json_numbers_are_one_source(self) -> None:
        calls = (
            ("reports/fetch", {"page": 1}, {"id": "zone-12345", "count": 2}),
            ("reports/fetch", {"page": 1.0}, {"id": "zone-12345", "count": 2.0}),
            ("reports/post", {"z": "zone-12345"}, {}),
        )
        recorded = _recorded(_send(*calls))
        self.assertEqual(_actions(recorded), ["fetch", "post"])
        self.assertEqual(_input(recorded)["z"], {"kind": "step_output", "step": "s1", "pointer": "/id"})

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
        named = "DNS de example.com e other.org a cada hora"
        split = _record(_send(ZONES_CALL, first, message=named), _send(second))
        self.assertEqual(split, recording.Question("routine-work-split"))
        whole = _recorded(_send(ZONES_CALL, first, message=named), _send(first, second))
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
        with mock.patch.object(routine_compose, "_resolved", return_value="other"):
            code = _code(self, lambda: _record(_send(ZONES_CALL, RECORDS, message=OWNER)))
        self.assertEqual(code, "routine-recording-unverified")

    def test_a_call_whose_input_differs_from_its_step_is_unverified(self) -> None:
        # Were two reads of different input one source, the later one's input would not be the step's.
        twins = _send(("reports/fetch", {}, {"a": 1}), ("reports/fetch", {"extra": 1}, {"a": 1}))
        with mock.patch.object(routine_compose, "_twins", return_value=True):
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
                send = _send(*calls, message=_then_hourly(known))
                protection = trace.Protection().grow((protected,))
                self.assertEqual(
                    _code(self, lambda s=send, p=protection: _record(s, protection=p)), "routine-secret-literal"
                )
                self.assertEqual(_input(_recorded(send))["zone_id"]["kind"], "step_output")

    def test_a_question_never_offers_a_target_or_label_the_span_protects(self) -> None:
        items = {
            "items": [
                {"name": "same", "id": "id-0001", "title": "tok-label-1"},
                {"name": "same", "id": "tok-secret-2"},
                {"name": "same", "id": "id-0003"},
            ]
        }
        calls = (("reports/fetch", {}, items), ("cloudflare/list-dns-records", {"zone_id": "id-0003"}, {}))
        unnamed = {"items": [{"id": "id-0001", "title": "tok-label-1"}, {"id": "id-0003", "title": "plain"}]}
        protection = trace.Protection().grow(("tok-secret-2", "tok-label-1"))
        asked = _record(_send(*calls, message=_then_hourly("same")), protection=protection)
        self.assertEqual(asked.options, ({"value": "id-0001", "label": "same"}, {"value": "id-0003", "label": "same"}))
        labelled = (("reports/fetch", {}, unnamed), ("cloudflare/list-dns-records", {"zone_id": "id-0003"}, {}))
        asked = _record(_send(*labelled, message=_then_hourly("x")), protection=protection)
        self.assertEqual(asked.options, ({"value": "id-0001", "label": None}, {"value": "id-0003", "label": "plain"}))

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


if __name__ == "__main__":
    unittest.main()
