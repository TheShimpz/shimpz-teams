"""Only an answer composed for exactly the pending Routine question binds one of its options (ADR-0092 section 2)."""

from __future__ import annotations

import unittest

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


def _question() -> routine_lineage.Question:
    return routine_lineage.Question(PRINCIPAL, ORIGINAL, QUESTION, LABELS, "create", None, ("news", "general"), "Done.")


def _answer(label: str, *, question: str = QUESTION, labels: tuple[str, str] = ("Pergunta", "Resposta")) -> str:
    return f"{ORIGINAL}\n\n{labels[0]}: {question}\n{labels[1]}: {label}"


class LineageTests(unittest.TestCase):
    def test_an_answer_selects_exactly_its_own_option_and_stays_retryable_until_settled(self) -> None:
        clock = Clock()
        book = routine_lineage.LineageBook(clock)
        book.record("team_1", _question())
        for label, index in (("#general", 1), ("#news", 0)):
            bound = book.bound("team_1", PRINCIPAL, _answer(label, labels=("Question", "Answer")))
            self.assertEqual((bound.index, bound.routine), (index, ("news", "general")[index]))
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

    def test_a_composed_answer_is_recognized_whether_or_not_it_binds(self) -> None:
        self.assertTrue(routine_lineage.composed(_answer("#general")))
        self.assertTrue(routine_lineage.composed(_answer("anything I typed")))
        self.assertFalse(routine_lineage.composed(ORIGINAL))
        self.assertFalse(routine_lineage.composed(_answer("#general") + "\nmore"))


if __name__ == "__main__":
    unittest.main()
