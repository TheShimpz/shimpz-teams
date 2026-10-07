"""A question's frozen work is settled only by a later send that runs it again exactly (ADR-0101)."""

from __future__ import annotations

import dataclasses
import datetime
import json
import unittest

from test_routine_recording import (
    RECORDS,
    SELECTED,
    SHIMPZ_ID,
    STARTED,
    TWINS,
    ZONES,
    ZONES_CALL,
    _actions,
    _asked,
    _code,
    _input,
    _pending,
    _post,
    _record,
    _recorded,
    _send,
)

from routine import recording, trace


class RerunTests(unittest.TestCase):
    """A question only work run again can answer stands until one later send repeats that work (ADR-0101)."""

    def test_an_unsourced_value_settles_only_when_one_later_send_looks_it_up_again(self) -> None:
        first = _send(RECORDS, message="DNS de shimpz.com a cada hora")
        asked = _record(first)
        self.assertEqual(asked.code, "routine-binding-unsourced")
        slot = recording.Slot(("cloudflare", "list-dns-records"), True, (("zone_id", "fresh", SHIMPZ_ID),))
        self.assertEqual(asked.manifest, recording.Manifest((slot,)))
        pending = _pending(asked, 1)
        # A send with no Action, one that only looks the zone up, or one that remembers it again settles nothing.
        for later in (_send(message="ok"), _send(ZONES_CALL), _send(RECORDS)):
            with self.subTest(later=later):
                self.assertEqual(_record(first, later, asked=pending).code, "routine-binding-unsourced")
        recorded = _recorded(first, _send(ZONES_CALL, RECORDS), asked=pending)
        self.assertEqual(
            (_actions(recorded), _input(recorded)["zone_id"]), (["list-zones", "list-dns-records"], SELECTED)
        )

    def test_split_work_settles_only_when_one_send_repeats_every_change_in_order(self) -> None:
        first = _send(_post("a"), message="a cada hora")
        asked = _record(first, _send(_post("b"), _post("b")))
        self.assertEqual(asked.code, "routine-work-split")
        changes = [slot.inputs for slot in asked.manifest.slots]
        self.assertEqual(changes, [(("t", "value", "a"),), (("t", "value", "b"),), (("t", "value", "b"),)])
        spans = (first, _send(_post("b"), _post("b")))
        pending = _pending(asked, 2)
        for later in (
            _send(message="ok"),
            _send(_post("a"), _post("b")),
            _send(_post("b"), _post("b"), _post("a")),
        ):
            with self.subTest(later=later):
                self.assertEqual(_record(*spans, later, asked=pending).code, "routine-work-split")
        recorded = _recorded(*spans, _send(_post("a"), _post("b"), _post("b")), asked=pending)
        self.assertEqual([step["input"]["t"]["value"] for step in recorded.document["steps"]], ["a", "b", "b"])

    def test_an_earlier_lookup_with_other_page_sizes_is_the_work_the_recording_ran_again(self) -> None:
        """The owner's list, then "do this every hour", when the agent picked other page sizes the second time."""

        def zones(per_page: int) -> tuple:
            return ("cloudflare/list-zones", {"page": 1, "per_page": per_page}, ZONES)

        def records(per_page: int) -> tuple:
            return ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID, "per_page": per_page}, RECORDS[2])

        first = _send(zones(50), records(25), message="Liste os registros DNS de shimpz.com")
        cases = {
            "only the zones' page size": (zones(5), records(25)),
            "both page sizes": (zones(5), records(10)),
            "the lookup after its use": (records(10), zones(5)),
        }
        for case, calls in cases.items():
            with self.subTest(case=case):
                recorded = _recorded(first, _send(*calls, message="Faça isso a cada hora"))
                zones_step, records_step = recorded.document["steps"]
                self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])
                # The work's own lookup is the source, at the page sizes the work used, and the records are shown.
                self.assertEqual(zones_step["input"]["per_page"], {"kind": "literal", "value": 5})
                self.assertEqual(records_step["input"]["zone_id"], SELECTED)
                sent = next(given for name, given, _result in calls if name == "cloudflare/list-dns-records")
                self.assertEqual(records_step["input"]["per_page"]["value"], sent["per_page"])
                self.assertEqual(recorded.document["output"]["step"], records_step["id"])

    def test_an_earlier_read_for_another_target_or_a_named_choice_is_still_split_work(self) -> None:
        other = "9a7806061c88ada191ed06f989cc3dac"
        work = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID, "per_page": 10}, RECORDS[2])
        cases = {
            # Another zone is a referable target, never the assistant's own choice.
            "another zone": ({"zone_id": other, "per_page": 25}, "Liste os registros DNS de example.com"),
            # A page size the person asked for is theirs, never superseded.
            "a named page size": ({"zone_id": SHIMPZ_ID, "per_page": 25}, "Liste os de shimpz.com com 25 por página"),
            # Inputs with other members never did the same work.
            "other members": ({"zone_id": SHIMPZ_ID}, "Liste os registros DNS de shimpz.com"),
        }
        for case, (given, message) in cases.items():
            with self.subTest(case=case):
                earlier = _send(("cloudflare/list-dns-records", given, RECORDS[2]), message=message)
                asked = _record(earlier, _send(ZONES_CALL, work, message="Faça isso a cada hora para shimpz.com"))
                self.assertEqual(asked.code, "routine-work-split")

    def test_work_split_with_a_withheld_input_is_never_frozen(self) -> None:
        for action in ("reports/post", "reports/fetch"):
            with self.subTest(action=action):
                hidden = (action, {"t": "x"}, {}, trace.Kept({"t": None}, frozenset({"/t"})))
                work = (action, {"t": "b"}, {})
                code = _code(self, lambda: _record(_send(hidden, message="a cada hora"), _send(work)))
                self.assertEqual(code, "routine-secret-literal")

    def test_two_identical_changes_rerun_once_settle_nothing(self) -> None:
        first = _send(_post("a"), message="a cada hora")
        second = _send(_post("b"), _post("b"))
        pending = _pending(_record(first, second), 2)
        self.assertEqual(
            _record(first, second, _send(_post("a"), _post("b")), asked=pending).code, "routine-work-split"
        )

    def test_a_run_date_settles_on_the_date_of_the_send_that_repeats_it(self) -> None:
        lookup = ("reports/fetch", {"q": "ids"}, {"id": "remembered-1"})
        dated = ("reports/fetch", {"day": "2026-10-05", "id": "remembered-1"}, {})
        first = _send(dated, message="relatório a cada hora")
        asked = _record(first)
        self.assertEqual(asked.manifest.slots[0].inputs, (("day", "clock", None), ("id", "fresh", "remembered-1")))
        pending = _pending(asked, 1)
        tomorrow = STARTED + 86_400
        next_day = ("reports/fetch", {"day": "2026-10-06", "id": "remembered-1"}, {})
        self.assertIsInstance(
            _record(first, _send(lookup, next_day, started_at=tomorrow), asked=pending), recording.Recorded
        )
        stale = _send(lookup, dated, started_at=tomorrow)
        self.assertEqual(_record(first, stale, asked=pending).code, "routine-binding-unsourced")

    def test_each_remembered_value_is_its_own_slot_and_a_rerun_must_repeat_each(self) -> None:
        other_id = ZONES["result"][1]["id"]
        other = ("cloudflare/list-dns-records", {"zone_id": other_id}, {"result": []})
        first = _send(RECORDS, other, message="DNS de shimpz.com e other.org a cada hora")
        asked = _record(first)
        self.assertEqual(len(asked.manifest.slots), 2)
        pending = _pending(asked, 1)
        for later in (_send(ZONES_CALL, RECORDS), _send(ZONES_CALL, RECORDS, RECORDS)):
            with self.subTest(later=later):
                self.assertEqual(_record(first, later, asked=pending).code, "routine-binding-unsourced")
        recorded = _recorded(first, _send(ZONES_CALL, RECORDS, other), asked=pending)
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records", "list-dns-records"])

    def test_read_only_twins_are_one_slot(self) -> None:
        remembered = ("cloudflare/list-dns-records", {"zone_id": "f" * 32}, {})
        asked = _record(_send(ZONES_CALL, ZONES_CALL, remembered, message="DNS a cada hora"))
        self.assertEqual(asked.code, "routine-binding-unsourced")
        self.assertEqual([slot.action[1] for slot in asked.manifest.slots], ["list-zones", "list-dns-records"])

    def test_a_rerun_admits_extra_calls_only_as_sources_and_keeps_the_frozen_order(self) -> None:
        first = _send(RECORDS, message="DNS de shimpz.com a cada hora")
        pending = _pending(_record(first), 1)
        unrelated = ("reports/fetch", {"q": 1}, {"x": 1})
        for extra in (unrelated, _post("x")):
            with self.subTest(extra=extra):
                later = _send(ZONES_CALL, RECORDS, extra)
                self.assertEqual(_record(first, later, asked=pending).code, "routine-binding-unsourced")
        read = ("reports/fetch", {"q": 1}, {})
        spans = (_send(_post("a"), message="a cada hora"), _send(read, _post("b")))
        pending = _pending(_record(*spans), 2)
        for moved in (_send(_post("a"), _post("b"), read), _send(read, _post("a"), _post("b"))):
            with self.subTest(moved=moved):
                self.assertEqual(_record(*spans, moved, asked=pending).code, "routine-work-split")
        self.assertIsInstance(_record(*spans, _send(_post("a"), read, _post("b")), asked=pending), recording.Recorded)

    def test_earlier_calls_for_targets_the_person_did_not_choose_are_no_split_work(self) -> None:
        # Records for two zones, then the person chose a third, shimpz.com, and the agent reran only that one.
        chosen, others = ZONES["result"][3], ZONES["result"][:2]
        calls = [("cloudflare/list-dns-records", {"zone_id": zone["id"]}, {"result": []}) for zone in (*others, chosen)]
        first = _send(ZONES_CALL, *calls[:2], message="A cada 5 segundos, liste minhas zonas")
        asked = _record(first)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        answer = _send(message=json.dumps(chosen["id"]))
        rerun = _record(first, answer, asked=_asked(asked, 1))
        self.assertEqual(rerun.code, "routine-work-rerun")
        again = _send(ZONES_CALL, *[calls[2]] * 3)
        # A record call for renamed work drops the rerun's manifest but keeps the person's choice.
        superseded = dataclasses.replace(_asked(rerun, 2), manifest=None)
        recorded = _recorded(first, answer, again, asked=superseded)
        self.assertEqual({step["input"]["zone_id"]["value"] for step in recorded.document["steps"][1:]}, {chosen["id"]})
        # Earlier calls for the unchosen targets stay eligible sources; they are only not split evidence.
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])

    def test_free_text_the_assistant_wrote_is_a_literal_in_the_frozen_work(self) -> None:
        typed = "some freely typed text"
        remembered = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID, "query": typed}, {"result": []})
        first = _send(remembered, message="DNS de shimpz.com a cada hora")
        asked = _record(first)
        self.assertEqual(asked.manifest.slots[0].inputs, (("query", "value", typed), ("zone_id", "fresh", SHIMPZ_ID)))
        recorded = _recorded(first, _send(ZONES_CALL, remembered), asked=_pending(asked, 1))
        self.assertEqual(_input(recorded)["query"], {"kind": "literal", "value": typed})

    def test_the_first_target_answer_already_excludes_the_unchosen_work(self) -> None:
        zone_a, zone_b = (zone["id"] for zone in ZONES["result"][:2])
        first = _send(ZONES_CALL, ("cloudflare/list-dns-records", {"zone_id": zone_a}, {}), message="DNS a cada hora")
        second = _send(("cloudflare/list-dns-records", {"zone_id": zone_b}, {}))
        asked = _record(first, second)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        answer = _send(message=json.dumps(zone_b))
        recorded = _recorded(first, second, answer, asked=_asked(asked, 2))
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "literal", "value": zone_b})
        repeated = _send(("cloudflare/list-dns-records", {"zone_id": zone_b}, {"n": 1}))
        recorded = _recorded(first, second, answer, repeated, asked=_asked(asked, 2))
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "literal", "value": zone_b})

    def test_settled_split_evidence_goes_but_an_earlier_source_stays(self) -> None:
        lookup = ("reports/fetch", {"q": "ids"}, {"id": "source-id-1"})
        spans = (_send(lookup, _post("a"), message="a cada hora"), _send(_post("b")))
        pending = _pending(_record(*spans), 2)
        settled = _send(_post("a"), _post("b"))
        work = _send(("cloudflare/list-dns-records", {"zone_id": "source-id-1"}, {}))
        recorded = _recorded(*spans, settled, work, asked=pending)
        self.assertEqual(_actions(recorded), ["fetch", "list-dns-records"])
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "step_output", "step": "s1", "pointer": "/id"})
        # New split work counts only the calls since the frontier.
        split = _record(*spans, settled, _send(_post("c")), asked=pending)
        self.assertEqual([slot.inputs[0][2] for slot in split.manifest.slots], ["a", "b", "c"])

    def test_a_date_the_person_named_stays_that_date_in_the_rerun(self) -> None:
        lookup = ("reports/fetch", {"q": "ids"}, {"id": "remembered-1"})
        dated = ("reports/fetch", {"day": "2026-10-05", "id": "remembered-1"}, {})
        first = _send(dated, message="Relatório de 2026-10-05 a cada hora")
        asked = _record(first)
        self.assertEqual(
            asked.manifest.slots[0].inputs, (("day", "value", "2026-10-05"), ("id", "fresh", "remembered-1"))
        )
        pending = _pending(asked, 1)
        tomorrow = STARTED + 86_400
        next_day = ("reports/fetch", {"day": "2026-10-06", "id": "remembered-1"}, {})
        self.assertEqual(_record(first, _send(lookup, next_day, started_at=tomorrow), asked=pending).code, asked.code)
        recorded = _recorded(first, _send(lookup, dated, started_at=tomorrow), asked=pending)
        self.assertEqual(_input(recorded)["day"], {"kind": "literal", "value": "2026-10-05"})

    def test_a_date_kept_as_a_literal_near_local_midnight_is_frozen_as_that_literal(self) -> None:
        # At 01:00 UTC it is still the day before in São Paulo, so the recorder keeps the UTC date as a literal.
        early = int(datetime.datetime(2026, 10, 5, 1, tzinfo=datetime.UTC).timestamp())
        lookup = ("reports/fetch", {"q": "ids"}, {"id": "remembered-1"})
        dated = ("reports/fetch", {"day": "2026-10-05", "id": "remembered-1"}, {})
        first = _send(dated, message="relatório a cada hora", started_at=early)
        asked = _record(first)
        self.assertEqual(
            asked.manifest.slots[0].inputs, (("day", "value", "2026-10-05"), ("id", "fresh", "remembered-1"))
        )
        recorded = _recorded(first, _send(lookup, dated, started_at=early + 86_400), asked=_pending(asked, 1))
        self.assertEqual(_input(recorded)["day"], {"kind": "literal", "value": "2026-10-05"})

    def test_the_settled_frontier_is_kept_while_another_question_is_asked(self) -> None:
        first = _send(RECORDS, message="DNS de shimpz.com")
        pending = _pending(_record(first), 1)
        rerun = _send(ZONES_CALL, RECORDS, message="pronto")
        asked = _record(first, rerun, asked=pending)
        self.assertEqual((asked.code, asked.frontier), ("routine-schedule-unstated", 1))
        answered = _recorded(
            first, rerun, _send(message="a cada hora"), asked=_pending(asked, 2), frontier=asked.frontier
        )
        self.assertEqual(_actions(answered), ["list-zones", "list-dns-records"])

    def test_a_chosen_target_stays_bound_while_another_question_is_asked(self) -> None:
        twins = ("cloudflare/list-zones", {}, TWINS)
        work = _send(twins, RECORDS, message="DNS de shimpz.com")
        asked = _record(work)
        chose = _record(work, _send(message=json.dumps(SHIMPZ_ID)), asked=_pending(asked, 1))
        self.assertEqual(
            (chose.code, [item.chosen for item in chose.chosen]), ("routine-schedule-unstated", [SHIMPZ_ID])
        )
        recorded = _recorded(
            work, _send(message=json.dumps(SHIMPZ_ID)), _send(message="a cada hora"), asked=_asked(chose, 2)
        )
        self.assertEqual(_input(recorded)["zone_id"], {"kind": "literal", "value": SHIMPZ_ID})


if __name__ == "__main__":
    unittest.main()
