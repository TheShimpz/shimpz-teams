"""The work a question asks the Brain to run again, and the one repair that work gets (ADR-0101)."""

import datetime
import json
import unittest

from test_routine_recording import (
    RECORDS,
    SELECTED,
    SHIMPZ_ID,
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

from routine import provenance as routine_provenance
from routine import recording, trace


class RepairTests(unittest.TestCase):
    """A question only work run again can answer stands until a later send runs Actions, which get one repair."""

    def test_an_unsourced_value_stands_until_work_runs_and_then_gets_one_repair(self) -> None:
        first = _send(RECORDS, message="DNS de shimpz.com a cada hora")
        asked = _record(first)
        self.assertEqual(asked.code, "routine-work-rerun")
        pending = _pending(asked, 1)
        # A send that runs no Action leaves the question standing.
        self.assertEqual(_record(first, _send(message="ok"), asked=pending).code, asked.code)
        # Work that remembers the value again ends the span instead of asking once more.
        for later in (_send(RECORDS), _send(RECORDS, RECORDS)):
            with self.subTest(later=later):
                code = _code(self, lambda later=later: _record(first, later, asked=pending))
                self.assertEqual(code, "routine-record-again")
        recorded = _recorded(first, _send(ZONES_CALL, RECORDS), asked=pending)
        self.assertEqual(
            (_actions(recorded), _input(recorded)["zone_id"]), (["list-zones", "list-dns-records"], SELECTED)
        )

    def test_work_rerun_with_other_words_records_the_latest_work_and_never_asks_about_a_split(self) -> None:
        """The owner's football Routine: each answer reran the search with other words."""

        def search(query: str) -> tuple:
            return ("reports/fetch", {"query": query, "num_results": 5}, {"results": [{"title": query}]})

        sends = (
            _send(search("jogos de futebol da semana"), message="Pesquise os jogos de futebol da semana"),
            _send(search("principais jogos de futebol esta semana"), message="Principais jogos"),
            _send(search("jogos futebol brasileirão e europa semana"), message="a cada 30 dias"),
        )
        recorded = _recorded(*sends)
        self.assertEqual(_actions(recorded), ["fetch"])
        self.assertEqual(_input(recorded)["query"]["value"], "jogos futebol brasileirão e europa semana")
        self.assertEqual(recorded.schedule, {"kind": "continuous", "gap": 30 * 86_400})

    def test_earlier_changes_are_never_split_work_the_latest_send_is_the_work(self) -> None:
        recorded = _recorded(_send(_post("a"), message="a cada hora"), _send(_post("b"), _post("b")))
        self.assertEqual([step["input"]["t"]["value"] for step in recorded.document["steps"]], ["b", "b"])

    def test_a_chosen_target_must_be_what_the_repair_sends(self) -> None:
        # Records for two zones, then the person chose a third, shimpz.com.
        chosen, others = ZONES["result"][3], ZONES["result"][:2]
        calls = [("cloudflare/list-dns-records", {"zone_id": zone["id"]}, {"result": []}) for zone in (*others, chosen)]
        first = _send(ZONES_CALL, *calls[:2], message="A cada 5 segundos, liste minhas zonas")
        asked = _record(first)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        answer = _send(message=json.dumps(chosen["id"]))
        rerun = _record(first, answer, asked=_asked(asked, 1))
        self.assertEqual(rerun.code, "routine-work-rerun")
        # Work run again for another zone, or still for the unchosen ones, ends the span.
        for later in (_send(ZONES_CALL, calls[0]), _send(ZONES_CALL, *calls[:2])):
            with self.subTest(later=later):
                code = _code(self, lambda later=later: _record(first, answer, later, asked=_asked(rerun, 2)))
                self.assertEqual(code, "routine-record-again")
        recorded = _recorded(first, answer, _send(ZONES_CALL, calls[2]), asked=_asked(rerun, 2))
        self.assertEqual(_actions(recorded), ["list-zones", "list-dns-records"])
        self.assertEqual({step["input"]["zone_id"]["value"] for step in recorded.document["steps"][1:]}, {chosen["id"]})

    def test_a_call_that_sends_nothing_or_a_withheld_value_never_sends_a_chosen_target(self) -> None:
        sent_nothing = _send(("reports/post", {}, {})).occurrences[0]
        withheld = _send(
            ("reports/post", {"text": "x"}, {}, trace.Kept({"text": None}, frozenset({"/text"})))
        ).occurrences[0]
        for occurrence in (sent_nothing, withheld):
            with self.subTest(occurrence=occurrence):
                self.assertIsNone(routine_provenance._sent_value(recording._Call(0, 0, occurrence), "text"))

    def test_a_repair_that_drops_the_chosen_target_ends_the_span(self) -> None:
        chosen, others = ZONES["result"][3], ZONES["result"][:2]
        calls = [("cloudflare/list-dns-records", {"zone_id": zone["id"]}, {"result": []}) for zone in (*others, chosen)]
        first = _send(ZONES_CALL, *calls[:2], message="A cada 5 segundos, liste minhas zonas")
        answer = _send(message=json.dumps(chosen["id"]))
        rerun = _record(first, answer, asked=_asked(_record(first), 1))
        # Neither a listing without a zone nor a zone another lookup returned is the zone the person chose.
        lookup = ("reports/fetch", {"q": "zone"}, {"id": "zone-from-lookup"})
        for later in (
            _send(ZONES_CALL, ("cloudflare/list-dns-records", {"zone_id": "z", "per_page": 5}, {})),
            _send(lookup, ("cloudflare/list-dns-records", {"zone_id": "zone-from-lookup"}, {})),
        ):
            with self.subTest(later=later):
                code = _code(self, lambda later=later: _record(first, answer, later, asked=_asked(rerun, 2)))
                self.assertEqual(code, "routine-record-again")

    def test_no_card_records_another_target_than_the_chosen_one_whatever_is_asked_between(self) -> None:
        chosen, others = ZONES["result"][3], ZONES["result"][:2]
        calls = [("cloudflare/list-dns-records", {"zone_id": zone["id"]}, {"result": []}) for zone in (*others, chosen)]
        first = _send(ZONES_CALL, *calls[:2], message="Liste minhas zonas")
        answer = _send(message=json.dumps(chosen["id"]))
        rerun = _record(first, answer, asked=_asked(_record(first), 1))
        lookup = ("reports/fetch", {"q": "zone"}, {"id": "zone-from-lookup"})
        other = _send(lookup, ("cloudflare/list-dns-records", {"zone_id": "zone-from-lookup"}, {}))
        schedule = _send(message="a cada hora")
        asked = recording.Asked("routine-schedule-unstated", 3, chosen=rerun.chosen, repair_from=rerun.repair_from)
        code = _code(self, lambda: _record(first, answer, other, schedule, asked=asked))
        self.assertEqual(code, "routine-record-again")

    def test_a_target_question_left_open_does_not_hold_back_other_work(self) -> None:
        twins = (
            "cloudflare/list-zones",
            {},
            {"result": [{"id": "zone-aaaa-1", "name": "a"}, {"id": "zone-bbbb-2", "name": "a"}]},
        )
        first = _send(
            twins, ("cloudflare/list-dns-records", {"zone_id": "zone-aaaa-1"}, {}), message="DNS de a a cada hora"
        )
        asked = _record(first)
        self.assertEqual(asked.code, "routine-binding-ambiguous")
        # The person typed no target, and the agent ran other work: the question it left open binds nothing.
        recorded = _recorded(first, _send(ZONES_CALL, message="na verdade, só as zonas"), asked=_asked(asked, 1))
        self.assertEqual(_actions(recorded), ["list-zones"])

    def test_an_earlier_dependency_of_the_work_keeps_its_own_ambiguity(self) -> None:
        zone = "zone-identifier-1"
        lookup = ("reports/fetch", {"q": "zones"}, {"zone": zone})
        report = ("reports/fetch", {"zone": zone}, {"report": "report-identifier-1"})
        read = ("reports/fetch", {"report": "report-identifier-1"}, {"meta": {"zone": zone}})
        asked = _record(_send(lookup, report, message="relatório"), _send(read, message="a cada hora"))
        self.assertEqual(asked.code, "routine-binding-ambiguous")

    def test_a_withheld_call_beside_a_rerun_question_is_never_named(self) -> None:
        hidden = ("reports/post", {"t": "x"}, {}, trace.Kept({"t": None}, frozenset({"/t"})))
        code = _code(self, lambda: _record(_send(hidden, RECORDS, message="DNS de shimpz.com a cada hora")))
        self.assertEqual(code, "routine-secret-literal")

    def test_a_withheld_input_is_never_frozen(self) -> None:
        for action in ("reports/post", "reports/fetch"):
            with self.subTest(action=action):
                hidden = (action, {"t": "x"}, {}, trace.Kept({"t": None}, frozenset({"/t"})))
                code = _code(self, lambda hidden=hidden: _record(_send(hidden, message="a cada hora")))
                self.assertEqual(code, "routine-secret-literal")


class ManifestTests(unittest.TestCase):
    """The work a rerun question names for the Brain: each call, and what each input must be."""

    def test_each_remembered_value_is_its_own_slot(self) -> None:
        other = ("cloudflare/list-dns-records", {"zone_id": ZONES["result"][1]["id"]}, {"result": []})
        asked = _record(_send(RECORDS, other, message="DNS de shimpz.com e other.org a cada hora"))
        self.assertEqual(len(asked.manifest.slots), 2)
        slot = recording.Slot(("cloudflare", "list-dns-records"), True, (("zone_id", "fresh", SHIMPZ_ID),))
        self.assertEqual(asked.manifest.slots[0], slot)

    def test_read_only_twins_are_one_slot(self) -> None:
        remembered = ("cloudflare/list-dns-records", {"zone_id": "f" * 32}, {})
        asked = _record(_send(ZONES_CALL, ZONES_CALL, remembered, message="DNS a cada hora"))
        self.assertEqual(asked.code, "routine-work-rerun")
        self.assertEqual([slot.action[1] for slot in asked.manifest.slots], ["list-zones", "list-dns-records"])

    def test_free_text_and_dates_are_named_as_the_recording_keeps_them(self) -> None:
        typed = "some freely typed text"
        remembered = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID, "query": typed}, {"result": []})
        asked = _record(_send(remembered, message="DNS de shimpz.com a cada hora"))
        self.assertEqual(asked.manifest.slots[0].inputs, (("query", "value", typed), ("zone_id", "fresh", SHIMPZ_ID)))
        dated = ("reports/fetch", {"day": "2026-10-05", "id": "remembered-1"}, {})
        run_date = _record(_send(dated, message="relatório a cada hora")).manifest.slots[0].inputs
        self.assertEqual(run_date, (("day", "clock", None), ("id", "fresh", "remembered-1")))
        named = _record(_send(dated, message="Relatório de 2026-10-05 a cada hora")).manifest.slots[0].inputs
        self.assertEqual(named, (("day", "value", "2026-10-05"), ("id", "fresh", "remembered-1")))

    def test_a_one_word_dated_text_is_named_as_the_recorder_keeps_it(self) -> None:
        # The recorder writes its date for each run; work run again sends it as it is, never a value to look up.
        dated = ("reports/fetch", {"name": "relatorio_2026-10-05", "id": "remembered-1"}, {})
        inputs = _record(_send(dated, message="relatório a cada hora")).manifest.slots[0].inputs
        self.assertEqual(inputs, (("id", "fresh", "remembered-1"), ("name", "value", "relatorio_2026-10-05")))
        recorded = _recorded(_send(("reports/post", {"text": "relatorio_2026-10-05"}, {})))
        self.assertEqual((_input(recorded)["text"]["kind"], recorded.origins["s1"]["text"]), ("run_clock", "clock"))

    def test_the_utc_date_of_another_local_day_is_named_as_that_literal(self) -> None:
        # At 01:00 UTC it is still the day before in São Paulo, so the recorder keeps the UTC date as a literal.
        early = int(datetime.datetime(2026, 10, 5, 1, tzinfo=datetime.UTC).timestamp())
        dated = ("reports/fetch", {"day": "2026-10-05", "id": "remembered-1"}, {})
        asked = _record(_send(dated, message="relatório a cada hora", started_at=early))
        self.assertEqual(
            asked.manifest.slots[0].inputs, (("day", "value", "2026-10-05"), ("id", "fresh", "remembered-1"))
        )


if __name__ == "__main__":
    unittest.main()
