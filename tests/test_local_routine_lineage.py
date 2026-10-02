"""A composed clarification answer's question and labels are never the user's own words (ADR-0092)."""

from __future__ import annotations

import unittest

from local.routine import lineage as routine_lineage
from routine.request import Request

PRINCIPAL = "a" * 32
ORIGINAL = "Every Monday, post the weekly report"
QUESTION = "Which channel should I post to?"
ANSWER = f"{ORIGINAL}\n\nPergunta: {QUESTION}\nResposta: #general"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _request(message: str = ORIGINAL, excluded: tuple[tuple[int, int], ...] = ()) -> Request:
    return Request(PRINCIPAL, message, 1, "b" * 32, excluded=excluded)


class LineageTests(unittest.TestCase):
    def test_an_answer_to_the_last_question_marks_only_its_question_line_and_answer_label(self) -> None:
        book = routine_lineage.LineageBook(Clock())
        book.record("team_1", _request(), QUESTION)
        spans = book.take("team_1", PRINCIPAL, ANSWER)
        marked = [ANSWER[start:end] for start, end in spans]
        self.assertEqual(marked, [f"Pergunta: {QUESTION}", "Resposta: "])
        # The lineage is consumed by the next message, and spans already marked in the original carry over.
        nested = f"{ANSWER}\n\nPergunta: When?\nResposta: 9:00"
        book.record("team_1", _request(ANSWER, spans), "When?")
        carried = book.take("team_1", PRINCIPAL, nested)
        self.assertEqual([nested[start:end] for start, end in carried[:2]], marked)
        self.assertEqual([nested[start:end] for start, end in carried[2:]], ["Pergunta: When?", "Resposta: "])

    def test_without_its_lineage_every_composed_looking_question_is_marked(self) -> None:
        clock = Clock()
        book = routine_lineage.LineageBook(clock)
        unbound = [ANSWER[start:end] for start, end in routine_lineage.unbound(ANSWER)]
        self.assertEqual(unbound, [f"Pergunta: {QUESTION}", "Resposta: "])
        self.assertEqual(book.take("team_1", PRINCIPAL, ANSWER), routine_lineage.unbound(ANSWER))
        for principal, message, advance in (
            ("c" * 32, ANSWER, 0),
            (PRINCIPAL, ANSWER, routine_lineage.LINEAGE_SECONDS),
            (PRINCIPAL, f"{ORIGINAL}!\n\nPergunta: {QUESTION}\nResposta: #general", 0),
            (PRINCIPAL, f"{ORIGINAL}\n\nPergunta: Another?\nResposta: #general", 0),
            (PRINCIPAL, f"{ORIGINAL}\n\nPergunta: {QUESTION}", 0),
            (PRINCIPAL, f"{ORIGINAL}\n\nPergunta: {QUESTION}\n#general", 0),
            (PRINCIPAL, f"{ORIGINAL}\n\nPergunta: {QUESTION}\n{'x' * 41}: #general", 0),
        ):
            with self.subTest(principal=principal, message=message, advance=advance):
                book.record("team_1", _request(), QUESTION)
                clock.now += advance
                self.assertEqual(book.take("team_1", principal, message), routine_lineage.unbound(message))
        self.assertEqual(routine_lineage.unbound(ORIGINAL), ())

    def test_a_team_or_a_space_reset_forgets_its_lineage(self) -> None:
        book = routine_lineage.LineageBook(Clock())
        book.record("team_1", _request(), QUESTION)
        book.record("team_2", _request(), QUESTION)
        book.drop("team_1")
        self.assertEqual(book.take("team_1", PRINCIPAL, ANSWER), routine_lineage.unbound(ANSWER))
        book.clear()
        self.assertEqual(book.take("team_2", PRINCIPAL, ANSWER), routine_lineage.unbound(ANSWER))
        book.record("team_3", _request(), QUESTION)
        self.assertEqual(len(book.take("team_3", PRINCIPAL, ANSWER)), 2)
