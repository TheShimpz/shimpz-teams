"""The person's own recent sends a Routine request may cite, frozen per send (ADR-0092 amendment, 2026-10-04)."""

from __future__ import annotations

import unittest

from local.routine import recent

EPOCH = 1_000


def _identity(issued_at: int, nonce: str) -> dict[str, object]:
    return {"issued_at": issued_at, "nonce": nonce * 32}


class RecentBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = [EPOCH]
        self.book = recent.RecentBook(now=lambda: self.clock[0])

    def send(self, issued_at: int, nonce: str, message: str, citable: bool = True, principal: str = "p") -> tuple:
        self.clock[0] = max(self.clock[0], issued_at)
        return self.book.admit("team_1", principal, _identity(issued_at, nonce), message, citable)

    def test_a_send_cites_the_consecutive_run_before_it_up_to_three_oldest_first(self) -> None:
        self.assertEqual(self.send(1_001, "a", "list my DNS zones"), ())
        self.assertEqual(self.send(1_002, "b", "do this every 30 seconds"), ("list my DNS zones",))
        for index, nonce in enumerate("cde"):
            self.send(1_003 + index, nonce, f"send {index}")
        self.assertEqual(self.send(1_010, "f", "repeat that"), ("send 0", "send 1", "send 2"))

    def test_a_barrier_ends_the_run_so_older_work_is_never_offered(self) -> None:
        self.send(1_001, "a", "send 100 to Ana")
        # A send with files, a composed answer, or text that cannot be cited exactly is a barrier.
        for index, (message, citable) in enumerate(
            (("summarize this file", False), ("  padded text", True), ("x" * 2_001, True), ("bad\x00", True))
        ):
            self.send(1_002 + 2 * index, chr(ord("b") + index), message, citable)
            self.assertEqual(self.send(1_003 + 2 * index, chr(ord("m") + index), "do this every day"), ())

    def test_a_resend_reuses_exactly_the_run_its_first_admission_froze(self) -> None:
        self.send(1_001, "a", "A")
        self.send(1_002, "b", "B")
        self.send(1_003, "c", "C")
        self.assertEqual(self.send(1_004, "d", "repeat the first of those"), ("A", "B", "C"))
        # Five later sends push A, B, and C out of the kept history; the frozen run is unchanged.
        for index, nonce in enumerate("efghi"):
            self.send(1_005 + index, nonce, f"later {index}")
        self.assertEqual(self.send(1_004, "d", "repeat the first of those"), ("A", "B", "C"))
        # The same identity with another message, or another person, cannot change a Routine at all.
        self.assertIsNone(self.send(1_004, "d", "something else"))
        self.assertIsNone(self.send(1_004, "d", "repeat the first of those", principal="q"))

    def test_sends_in_the_same_second_keep_their_admission_order(self) -> None:
        self.send(1_001, "a", "list my DNS zones")
        self.assertEqual(self.send(1_001, "b", "do this every 30 seconds"), ("list my DNS zones",))

    def test_an_identity_team_cannot_place_cites_nothing_and_becomes_a_barrier(self) -> None:
        self.send(1_001, "a", "send 100 to Ana")
        # Issued before this record began, as a retry after a restart would be: it cites nothing, and nothing older is
        # ever cited past it, even though its own text was citable.
        self.assertEqual(self.send(EPOCH, "b", "list my zones"), ())
        self.assertEqual(self.send(1_002, "c", "do that daily"), ())
        # An identity no longer fresh is placed nowhere either.
        self.clock[0] = 2_000 + recent.FROZEN_SECONDS
        self.assertEqual(self.send(2_000, "d", "list my zones"), ())
        self.assertEqual(self.send(self.clock[0], "e", "do that daily"), ())

    def test_live_runs_are_never_evicted_and_a_full_book_refuses_the_change(self) -> None:
        book = recent.RecentBook(now=lambda: EPOCH)
        identity = {"issued_at": EPOCH + 1, "nonce": "f" * 32}
        book.admit("team_1", "p", {"issued_at": EPOCH + 1, "nonce": "e" * 32}, "list my DNS zones", True)
        self.assertEqual(book.admit("team_1", "p", identity, "do this", True), ("list my DNS zones",))
        for index in range(recent.MAX_FROZEN - 2):
            book.admit("team_1", "p", {"issued_at": EPOCH + 1, "nonce": f"{index:032x}"}, "x", True)
        self.assertIsNone(book.admit("team_1", "p", {"issued_at": EPOCH + 1, "nonce": "d" * 32}, "y", True))
        self.assertEqual(book.admit("team_1", "p", identity, "do this", True), ("list my DNS zones",))

    def test_a_send_refused_at_capacity_stays_refused_and_never_acquires_later_history(self) -> None:
        clock = [EPOCH + 800]
        book = recent.RecentBook(now=lambda: EPOCH)
        book._now = lambda: clock[0]
        # A full book of live runs, admitted late in their identities' freshness.
        for index in range(recent.MAX_FROZEN):
            book.admit("team_1", "p", {"issued_at": EPOCH + 1, "nonce": f"{index:032x}"}, "x", True)
        refused = {"issued_at": EPOCH + 860, "nonce": "f" * 32}
        self.assertIsNone(book.admit("team_1", "p", refused, "do this every 30 seconds", True))
        # Every live run expires, newer work is sent, and the refused identity, still fresh, is retried.
        clock[0] = EPOCH + 1_000
        self.assertEqual(
            book.admit("team_1", "p", {"issued_at": clock[0] + 1, "nonce": "e" * 32}, "send 100", True), ()
        )
        self.assertIsNone(book.admit("team_1", "p", refused, "do this every 30 seconds", True))

    def test_placement_follows_the_canonical_identity_freshness(self) -> None:
        self.send(1_001, "a", "A")
        self.clock[0] = 1_002 + 899
        self.assertEqual(self.send(1_002, "b", "again"), ("A",))
        # At 900 seconds an identity is no longer fresh, and one issued too far ahead of Team is not yet.
        self.clock[0] = 1_003 + 900
        self.assertEqual(self.send(1_003, "c", "list my zones"), ())
        self.assertEqual(self.send(self.clock[0], "e", "do that"), ())

    def test_an_identity_issued_too_far_ahead_is_refused_for_good(self) -> None:
        self.clock[0] = 1_001
        ahead = _identity(1_001 + recent.http_payload.REQUEST_IDENTITY_SKEW_SECONDS + 1, "a")
        self.assertIsNone(self.book.admit("team_1", "p", ahead, "do this every 30 seconds", True))
        # Newer work is sent; once the early identity is fresh, its retry still changes no Routine.
        self.clock[0] = 1_003
        self.book.admit("team_1", "p", _identity(1_003 + 60, "b"), "send 100 to Ana", True)
        self.clock[0] = 1_004
        self.assertIsNone(self.book.admit("team_1", "p", ahead, "do this every 30 seconds", True))

    def test_a_frozen_run_expires_with_its_identity(self) -> None:
        self.send(1_001, "a", "A")
        self.assertEqual(self.send(1_002, "b", "again"), ("A",))
        self.clock[0] = 1_002 + recent.FROZEN_SECONDS
        self.assertEqual(self.send(1_002, "b", "again"), ())

    def test_deleting_or_recreating_a_team_or_resetting_forgets_every_send_and_run(self) -> None:
        self.send(1_001, "a", "A")
        self.book.admit("team_2", "p", _identity(1_001, "a"), "B", True)
        self.book.drop("team_1")
        self.assertEqual(self.send(1_002, "b", "again"), ())
        # Only identities issued after the drop are placed again.
        self.assertEqual(self.send(1_001, "z", "again"), ())
        self.assertEqual(self.book.admit("team_2", "p", _identity(1_002, "b"), "again", True), ("B",))
        self.clock[0] = 1_003
        self.book.clear()
        self.assertEqual(self.book.admit("team_2", "p", _identity(1_004, "d"), "again", True), ())
        self.assertEqual(self.book.admit("team_2", "p", _identity(1_003, "c"), "again", True), ())


if __name__ == "__main__":
    unittest.main()
