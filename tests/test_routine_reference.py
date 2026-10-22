"""Which Routine a recording send selects, and what its Brain then receives of its calls (ADR-0122)."""

import dataclasses
import unittest

from test_local_routine_recorder import BINDING, _occurrence, _started

from local.routine import recorder as routine_recorder
from routine import reference as routine_reference

DNS = {"routine_id": "a" * 32, "name": "DNS de shimpz.com"}
NEWS = {"routine_id": "b" * 32, "name": "Notícias"}
SHORT = {"routine_id": "c" * 32, "name": "DNS"}


class ReferenceTests(unittest.TestCase):
    def test_a_refine_entry_selects_its_listed_routine_and_nothing_else(self) -> None:
        listed = (DNS, NEWS)
        self.assertEqual(routine_reference.referred(listed, ["notícias"], "a" * 32), ("selected", "a" * 32))
        self.assertEqual(routine_reference.referred(listed, [], "f" * 32), ("unresolved", None))

    def test_the_persons_words_name_one_routine_without_case_or_composition(self) -> None:
        listed = (DNS, NEWS)
        decomposed = "Na rotina NOTÍCIAS, inclua esportes"
        self.assertEqual(routine_reference.referred(listed, [decomposed], None), ("selected", "b" * 32))
        self.assertEqual(
            routine_reference.referred(listed, ["Ajuste dns de SHIMPZ.com."], None), ("selected", "a" * 32)
        )

    def test_a_name_inside_a_longer_word_names_nothing(self) -> None:
        self.assertEqual(routine_reference.referred((SHORT,), ["os DNSSEC estão ok"], None), ("none", None))
        self.assertEqual(routine_reference.referred((SHORT,), ["ver o DNS!"], None), ("selected", "c" * 32))

    def test_several_names_resolve_to_none_and_no_name_says_nothing(self) -> None:
        listed = (DNS, NEWS, SHORT)
        self.assertEqual(routine_reference.referred(listed, ["DNS e Notícias"], None), ("unresolved", None))
        self.assertEqual(routine_reference.referred(listed, ["DNS de shimpz.com"], None), ("unresolved", None))
        self.assertEqual(routine_reference.referred(listed, ["Bom dia"], None), ("none", None))


def _span_book(*sends: tuple[tuple[str, int], ...], reference=(routine_reference.NONE, None)):
    book = routine_recorder.RecordingBook(clock=lambda: 1)
    send = book.start("team_1", BINDING, _started(), 1)
    book.listed("team_1", send, (("a" * 32, 2), ("b" * 32, 1)), reference)
    return book, send


class SelectionTests(unittest.TestCase):
    def test_a_send_that_names_nothing_keeps_the_spans_selection_while_it_is_listed(self) -> None:
        book, first = _span_book(reference=(routine_reference.SELECTED, "a" * 32))
        self.assertEqual(book.get("team_1", first).selected, "a" * 32)
        self.assertTrue(routine_recorder.routine_mode(book.get("team_1", first)))
        second = book.start("team_1", BINDING, _started(), 2)
        book.listed("team_1", second, (("a" * 32, 3),))
        self.assertEqual(book.get("team_1", second).selected, "a" * 32)
        third = book.start("team_1", BINDING, _started(), 3)
        book.listed("team_1", third, (("b" * 32, 1),))
        self.assertIsNone(book.get("team_1", third).selected)

    def test_another_routine_or_an_unresolved_reference_replaces_the_selection(self) -> None:
        book, first = _span_book(reference=(routine_reference.SELECTED, "a" * 32))
        second = book.start("team_1", BINDING, _started(), 2)
        book.listed("team_1", second, (("a" * 32, 2), ("b" * 32, 1)), (routine_reference.SELECTED, "b" * 32))
        self.assertEqual(book.get("team_1", second).selected, "b" * 32)
        third = book.start("team_1", BINDING, _started(), 3)
        book.listed("team_1", third, (("a" * 32, 2), ("b" * 32, 1)), (routine_reference.UNRESOLVED, None))
        self.assertIsNone(book.get("team_1", third).selected)
        self.assertFalse(routine_recorder.routine_mode(book.get("team_1", third)))
        self.assertIsNone(book.get("team_1", first))

    def test_results_name_the_traced_calls_only_while_a_routine_is_selected(self) -> None:
        book, send = _span_book()
        first = dataclasses.replace(_occurrence(), interrupt="i-1")
        second = dataclasses.replace(_occurrence(), interrupt="i-3")
        book.occurred("team_1", send, first)
        book.occurred("team_1", send, second)
        results = {"i-1": {"zones": []}, "i-2": {"error": "failed"}, "i-3": [1]}
        self.assertEqual(routine_recorder.labeled(book.get("team_1", send), results), results)
        self.assertEqual(routine_recorder.labeled(None, results), results)
        book.listed("team_1", send, (("a" * 32, 2),), (routine_reference.SELECTED, "a" * 32))
        self.assertEqual(
            routine_recorder.labeled(book.get("team_1", send), results),
            {
                "i-1": {"call": "c1", "result": {"zones": []}},
                "i-2": {"error": "failed"},
                "i-3": {"call": "c2", "result": [1]},
            },
        )


if __name__ == "__main__":
    unittest.main()
