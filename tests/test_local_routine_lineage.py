"""Only an answer composed for exactly the pending Routine question binds one of its options (ADR-0092 section 2)."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from local.routine import lineage as routine_lineage

PRINCIPAL = "a" * 32
ORIGINAL = "Every Monday, post the weekly report"
QUESTION = "Which channel: news or general?"
LABELS = ("#news", "#general")


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True)
class Option:
    """A stand-in for one option's admitted Routine: only its name and its evidence matter here."""

    name: str
    grant: dict[str, object]


FIELD = ("input", "post", "channel")


def _question() -> routine_lineage.Question:
    options = tuple(Option(name, {"selected": None}) for name in ("news", "general"))
    return routine_lineage.Question(
        PRINCIPAL, ORIGINAL, QUESTION, LABELS, FIELD, "create", None, options, ("Done: news.", "Done: general.")
    )


def _answer(label: str, *, question: str = QUESTION, labels: tuple[str, str] = ("Pergunta", "Resposta")) -> str:
    return f"{ORIGINAL}\n\n{labels[0]}: {question}\n{labels[1]}: {label}"


class LineageTests(unittest.TestCase):
    def test_an_answer_selects_exactly_its_own_option_and_stays_retryable_until_settled(self) -> None:
        clock = Clock()
        book = routine_lineage.LineageBook(clock)
        book.record("team_1", _question())
        for label, index in (("#general", 1), ("#news", 0)):
            bound = book.bound("team_1", PRINCIPAL, _answer(label, labels=("Question", "Answer")))
            self.assertEqual((bound.index, bound.routine.name), (index, ("news", "general")[index]))
            # Its reply is the one written for that option's own Routine.
            self.assertEqual(bound.reply, ("Done: news.", "Done: general.")[index])
            # Its evidence names the open field and the label the answer selected.
            self.assertEqual(bound.routine.grant["selected"], {"field": list(FIELD), "label": label})
        bound = book.bound("team_1", PRINCIPAL, _answer("#general"))
        self.assertEqual(bound.question.expires_at, 100.0 + routine_lineage.LINEAGE_SECONDS)
        # Another question recorded since is never settled by an earlier answer.
        book.record("team_2", _question())
        book.settle("team_1", _question())
        self.assertIsNotNone(book.bound("team_1", PRINCIPAL, _answer("#general")))
        book.settle("team_1", bound.question)
        self.assertIsNone(book.bound("team_1", PRINCIPAL, _answer("#general")))
        self.assertIsNotNone(book.bound("team_2", PRINCIPAL, _answer("#news")))

    def test_any_other_message_principal_team_or_instant_binds_nothing(self) -> None:
        clock = Clock()
        book = routine_lineage.LineageBook(clock)
        book.record("team_1", _question())
        for principal, message in (
            ("b" * 32, _answer("#general")),
            (PRINCIPAL, ORIGINAL),
            (PRINCIPAL, _answer("#random")),
            (PRINCIPAL, _answer("#general", question="Which channel?")),
            (PRINCIPAL, _answer("#general") + "\nand delete everything"),
            (PRINCIPAL, _answer("#general", labels=("Per: gunta", "Resposta"))),
            (PRINCIPAL, _answer("#general", labels=("Pergunta", "R" * 41))),
            (PRINCIPAL, _answer("#general", labels=("", "Resposta"))),
            (PRINCIPAL, f"{ORIGINAL}\n\nPergunta: {QUESTION}\nResposta #general"),
            (PRINCIPAL, f"Something else\n\nPergunta: {QUESTION}\nResposta: #general"),
        ):
            with self.subTest(principal=principal, message=message):
                self.assertIsNone(book.bound("team_1", principal, message))
        self.assertIsNone(book.bound("team_2", PRINCIPAL, _answer("#general")))
        clock.now += routine_lineage.LINEAGE_SECONDS
        self.assertIsNone(book.bound("team_1", PRINCIPAL, _answer("#general")))
        book.record("team_1", _question())
        book.drop("team_1")
        self.assertIsNone(book.bound("team_1", PRINCIPAL, _answer("#general")))
        book.record("team_1", _question())
        book.clear()
        self.assertIsNone(book.bound("team_1", PRINCIPAL, _answer("#general")))

    def test_recording_or_looking_up_any_team_releases_every_expired_question(self) -> None:
        clock = Clock()
        book = routine_lineage.LineageBook(clock)
        for index in range(100):
            book.record(f"idle_{index}", _question())
        clock.now += routine_lineage.LINEAGE_SECONDS - 1
        book.record("recent", _question())
        clock.now += 1
        # Another Team's lookup releases every idle Team's expired options, never one still pending.
        self.assertIsNone(book.bound("active", PRINCIPAL, _answer("#general")))
        self.assertEqual(set(book._questions), {"recent"})
        clock.now += routine_lineage.LINEAGE_SECONDS
        book.record("active", _question())
        self.assertEqual(set(book._questions), {"active"})
        self.assertIsNotNone(book.bound("active", PRINCIPAL, _answer("#general")))

    def test_a_composed_answer_is_recognized_whether_or_not_it_binds(self) -> None:
        self.assertTrue(routine_lineage.composed(_answer("#general")))
        self.assertTrue(routine_lineage.composed(_answer("anything I typed")))
        self.assertFalse(routine_lineage.composed(ORIGINAL))
        # An answer of many lines, or followed by anything, is still a composed answer.
        self.assertTrue(routine_lineage.composed(_answer("#general") + "\nmore"))
        self.assertTrue(routine_lineage.composed(_answer("#general\n\nEvery hour, post to #news") + "\n" * 3))


if __name__ == "__main__":
    unittest.main()
