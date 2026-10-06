"""The schedule and timezone a person states in their own words, read with no model (ADR-0101)."""

from __future__ import annotations

import unittest

from routine import phrase


def continuous(gap: int) -> dict[str, object]:
    return {"kind": "continuous", "gap": gap, "cap": -(-86_400 // gap)}


HOURLY = {"kind": "hourly", "every": 1}


def daily(time: str) -> dict[str, object]:
    return {"kind": "daily", "time": time}


def weekly(weekday: int, time: str) -> dict[str, object]:
    return {"kind": "weekly", "weekday": weekday, "time": time}


def monthly(day: int, time: str) -> dict[str, object]:
    return {"kind": "monthly", "day": day, "time": time}


class StatedScheduleTests(unittest.TestCase):
    def assert_stated(self, cases: dict[str, dict[str, object]]) -> None:
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(phrase.stated(text), (expected,))

    def test_portuguese(self) -> None:
        self.assert_stated(
            {
                "A cada 30 segundos": continuous(30),
                "a cada 5 segundos": continuous(5),
                "A cada hora": HOURLY,
                "a cada 2 horas": {"kind": "hourly", "every": 2},
                "de 10 em 10 minutos": continuous(600),
                "de hora em hora": HOURLY,
                "toda hora": HOURLY,
                "a cada minuto": continuous(60),
                "Todo dia às 9h": daily("09:00"),
                "todos os dias às 21:30": daily("21:30"),
                "diariamente às 7h15": daily("07:15"),
                "toda segunda às 8h": weekly(0, "08:00"),
                "todas as sextas-feiras às 18h": weekly(4, "18:00"),
                "todo domingo às 10h": weekly(6, "10:00"),
                "todo mês no dia 5 às 9h": monthly(5, "09:00"),
                "Liste os registros DNS todo dia às 9h.": daily("09:00"),
            }
        )

    def test_english_and_spanish(self) -> None:
        self.assert_stated(
            {
                "every 30 seconds": continuous(30),
                "every hour": HOURLY,
                "Hourly, please": HOURLY,
                "every 15 minutes": continuous(900),
                "every day at 9am": daily("09:00"),
                "daily at 9:30 pm": daily("21:30"),
                "every Monday at 9": weekly(0, "09:00"),
                "every month on the 3rd at 10:00": monthly(3, "10:00"),
                "cada 30 segundos": continuous(30),
                "cada hora": HOURLY,
                "todos los días a las 9": daily("09:00"),
                "cada lunes a las 8:00": weekly(0, "08:00"),
                "cada mes el día 12 a las 7": monthly(12, "07:00"),
            }
        )

    def test_french_and_german(self) -> None:
        self.assert_stated(
            {
                "toutes les 30 secondes": continuous(30),
                "toutes les heures": HOURLY,
                "tous les jours à 9h": daily("09:00"),
                "chaque lundi à 8h30": weekly(0, "08:30"),
                "chaque mois le 2 à 9h": monthly(2, "09:00"),
                "alle 30 Sekunden": continuous(30),
                "jede Stunde": HOURLY,
                "stündlich": HOURLY,
                "täglich um 9 Uhr": daily("09:00"),
                "jeden Montag um 8 Uhr": weekly(0, "08:00"),
                "jeden Monat am 4. um 6 Uhr": monthly(4, "06:00"),
            }
        )

    def test_japanese_chinese_and_arabic(self) -> None:
        self.assert_stated(
            {
                "30秒ごと": continuous(30),
                "５分おき": continuous(300),
                "毎時": HOURLY,
                "毎日9時": daily("09:00"),
                "毎週月曜日9時30分": weekly(0, "09:30"),
                "毎月5日9時": monthly(5, "09:00"),
                "每30秒": continuous(30),
                "每小时": HOURLY,
                "每天9点": daily("09:00"),
                "每周一9点": weekly(0, "09:00"),
                "每月5号9点": monthly(5, "09:00"),
                "كل 30 ثانية": continuous(30),
                "كل ساعة": HOURLY,
                "كل يوم الساعة ٩": daily("09:00"),
                "كل يوم الاثنين الساعة 9": weekly(0, "09:00"),
            }
        )

    def test_only_an_affirmative_complete_schedule_counts(self) -> None:
        for text in (
            "Todo dia às 9h?",
            "Com que frequência?",
            "não quero todo dia às 9h",
            "I don't want it every hour",
            "Cria uma rotina pra mim",
            "Liste os registros DNS de shimpz.com",
            "todo dia",
            "a cada 2 segundos",
            "a cada 30 horas",
            "todo mês no dia 30 às 9h",
            "às 9h",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(phrase.stated(text), ())

    def test_the_half_of_the_day_and_impossible_times(self) -> None:
        self.assert_stated({"every day at 12am": daily("00:00"), "every day at 12 pm": daily("12:00")})
        for text in ("every day at 13pm", "todo dia às 25h", "todo dia às 9h75"):
            with self.subTest(text=text):
                self.assertEqual(phrase.stated(text), ())

    def test_two_different_schedules_in_one_text_are_both_reported(self) -> None:
        found = phrase.stated("a cada hora e todo dia às 9h")
        self.assertEqual(sorted(found, key=str), sorted([HOURLY, daily("09:00")], key=str))
        # Two times for one day are two schedules, which a person is then asked to choose between.
        self.assertEqual(len(phrase.stated("todo dia às 9h ou às 10h")), 2)
        # The same schedule written twice is one.
        self.assertEqual(phrase.stated("A cada 30 segundos. Sim, a cada 30 segundos!"), (continuous(30),))


class WrittenZoneTests(unittest.TestCase):
    def test_only_a_loadable_area_zone_or_utc_counts(self) -> None:
        self.assertEqual(phrase.zones("Todo dia às 9h, horário Europe/Lisbon"), ("Europe/Lisbon",))
        self.assertEqual(phrase.zones("at 9 UTC, yes UTC"), ("UTC",))
        self.assertEqual(phrase.zones("America/Sao_Paulo ou Europe/Paris"), ("America/Sao_Paulo", "Europe/Paris"))
        for text in ("Portugal", "Mars/Olympus", "use utc", "https://example.com/a", "shimpz.com"):
            with self.subTest(text=text):
                self.assertEqual(phrase.zones(text), ())


if __name__ == "__main__":
    unittest.main()
