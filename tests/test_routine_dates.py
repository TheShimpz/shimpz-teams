"""Dates inside a recorded text, which each run writes again for its own date (ADR-0101)."""

import datetime
import unittest

from routine import dates

RECORDED_ON = datetime.date(2026, 10, 15)
TOMORROW = datetime.date(2026, 10, 16)


def _template(text: str, locale: str | None = "pt", named: frozenset = frozenset()) -> list | None:
    return dates.template(text, RECORDED_ON, locale, named.__contains__)


class RecognitionTests(unittest.TestCase):
    def test_each_written_form_moves_with_the_run_date(self) -> None:
        cases = {
            ("notícias IA hoje 15 outubro 2026", "pt"): "notícias IA hoje 16 outubro 2026",
            ("notícias de 15 de outubro de 2026", "pt"): "notícias de 16 de outubro de 2026",
            ("jogos 15/10/2026 a 21/10/2026", "pt"): "jogos 16/10/2026 a 22/10/2026",
            ("report 2026-10-15", "pt"): "report 2026-10-16",
            ("weather 5/10", "pt"): "weather 6/10",
            ("AI news October 15, 2026", "en"): "AI news October 16, 2026",
            ("news 10/14/2026", "en"): "news 10/15/2026",
            ("Nachrichten 15. Oktober 2026", "de"): "Nachrichten 16. Oktober 2026",
            ("actualités 15 octobre 2026", "fr"): "actualités 16 octobre 2026",
            ("ニュース 2026年10月15日", "ja"): "ニュース 2026年10月16日",
            ("أخبار 15 أكتوبر 2026", "ar"): "أخبار 16 أكتوبر 2026",
            ("NOTÍCIAS 15 OUTUBRO", "pt"): "NOTÍCIAS 16 OUTUBRO",
            ("news 31/10/2026", "pt"): "news 01/11/2026",
        }
        for (text, locale), tomorrow in cases.items():
            with self.subTest(text=text):
                parts = _template(text, locale)
                self.assertEqual(dates.render(parts, RECORDED_ON), text)
                self.assertEqual(dates.render(parts, TOMORROW), tomorrow)
                self.assertTrue(dates.admitted(parts))

    def test_text_without_a_movable_date_stays_a_literal(self) -> None:
        for text in (
            "notícias de inteligência artificial",
            "notícias outubro 2026",
            "relatório 2026",
            "15 de abril de 2026",
            "1/2 xícara",
            "jogos de 15 a 21/10/2026",
            "15/10/2026 a 3 dias",
            "oUtubro 15",
        ):
            with self.subTest(text=text):
                self.assertIsNone(_template(text))

    def test_a_reading_two_languages_or_orders_share_is_the_interface_languages_or_none(self) -> None:
        # "10/10/2026" reads day first or month first; "agosto" is Portuguese and Spanish.
        self.assertEqual(_template("news 10/10/2026", "pt")[1]["format"], "{dd}/{MM}/{yyyy}")
        self.assertEqual(_template("news 10/10/2026", "en")[1]["format"], "{MM}/{dd}/{yyyy}")
        self.assertIsNone(_template("news 10/10/2026", None))
        near_august = datetime.date(2026, 8, 20)
        self.assertEqual(dates.template("15 de agosto", near_august, "es", lambda _day: False)[0]["lang"], "es")
        self.assertIsNone(dates.template("15 de agosto", near_august, "fr", lambda _day: False))

    def test_overlapping_forms_read_the_first_form_once(self) -> None:
        parts = _template("news 15 October 15, 2026", "en")
        self.assertEqual(dates.render(parts, TOMORROW), "news 16 October 15, 2026")

    def test_a_date_the_person_wrote_stays_as_written(self) -> None:
        named = frozenset({datetime.date(2026, 10, 20)})
        parts = _template("jogos 15/10/2026 e 20/10/2026", named=named)
        self.assertEqual(dates.render(parts, TOMORROW), "jogos 16/10/2026 e 20/10/2026")
        self.assertIsNone(_template("jogo 20/10/2026", named=named))
        self.assertEqual(dates.dates("o jogo de 20/10", RECORDED_ON, "pt"), {datetime.date(2026, 10, 20)})


class ShapeTests(unittest.TestCase):
    def test_a_template_is_closed(self) -> None:
        dated = {"days": 0, "format": "{d}", "lang": None}
        for parts in (
            [],
            ["text"],
            ["a", "b", dated],
            ["", dated],
            [{"days": 32, "format": "{d}", "lang": None}],
            [{"days": True, "format": "{d}", "lang": None}],
            [{"days": 0, "format": "{x}", "lang": None}],
            [{"days": 0, "format": "plain", "lang": None}],
            [{"days": 0, "format": "{mmmm}", "lang": None}],
            [{"days": 0, "format": "{d}", "lang": "pt"}],
            [{"days": 0, "format": "{d}" * 22, "lang": None}],
            [{"days": 0, "format": "{d}"}],
            "{d}",
            [dated] * (dates.MAX_PARTS + 1),
        ):
            with self.subTest(parts=parts):
                self.assertFalse(dates.admitted(parts))

    def test_a_person_reads_each_date_by_its_format_language_and_offset(self) -> None:
        parts = [
            "{x} ",
            {"days": 0, "format": "{d} de {mmmm}", "lang": "pt"},
            " a ",
            {"days": 6, "format": "{dd}/{MM}", "lang": None},
        ]
        self.assertEqual(dates.shown(parts), "{{x}} {D de month|pt} a {DD/MM|+6}")


if __name__ == "__main__":
    unittest.main()
