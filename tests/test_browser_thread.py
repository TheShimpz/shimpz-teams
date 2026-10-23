"""A Browser task's thread: folds aim at their target, the memory is replaced whole, and every bound holds."""

import unittest

from browser import thread as browser_thread
from protocol.http.v1 import browser as browser_protocol


def _entry(text: str, note: str | None = None) -> dict[str, object]:
    step = {"kind": "wait"} if note is None else {"kind": "wait", "note": note}
    page = {"url": "https://example.com", "title": "Example"}
    return {"step": step, "outcome": "ok", "page": page, "text": text, "downloads": None, "screenshot": False}


def _record(note: str) -> dict[str, object]:
    return {key: value for key, value in _entry("", note).items() if key in ("step", "outcome", "page")}


class ThreadTests(unittest.TestCase):
    def test_a_fold_stops_once_the_history_reaches_its_target(self):
        thread = browser_thread.Thread(steps=[_entry("x" * 12_000) for _ in range(10)])
        count = thread.to_fold(9_600)
        self.assertGreater(count, 0)
        self.assertLess(count, len(thread.steps))

    def test_full_pages_fold_only_every_few_steps_with_a_full_memory(self):
        # Real pages fill their observation bound; with the memory and folded lines at theirs, a fold must still leave
        # room for more than one page, or the agent rewrites its memory at every step.
        lines = browser_protocol.MAX_FOLDED_BYTES // browser_protocol.record_bytes(_record("n" * 300))
        thread = browser_thread.Thread(
            folded=[_record("n" * 300)] * lines, memory="m" * browser_protocol.MAX_MEMORY_BYTES
        )
        folds = 0
        for _ in range(30):
            entry = _entry("x" * browser_protocol.MAX_OBSERVATION_BYTES)
            count = thread.to_fold(browser_protocol.entry_bytes(entry))
            if count:
                folds += 1
                thread.fold(count, "m" * browser_protocol.MAX_MEMORY_BYTES)
            thread.append(entry)
            self.assertIsNotNone(browser_protocol.canonical_thread(thread.wire()))
        self.assertLessEqual(folds, 30 // 3)

    def test_a_fold_replaces_the_memory_with_the_rewritten_one(self):
        thread = browser_thread.Thread(memory="V101: R$ 9.500", steps=[_entry("x"), _entry("y")])
        thread.fold(1, "V101: R$ 9.500\nV102: R$ 7.000")
        self.assertEqual(thread.memory, "V101: R$ 9.500\nV102: R$ 7.000")
        self.assertEqual(len(thread.folded), 1)

    def test_a_fold_without_a_memory_keeps_the_old_one_and_names_the_gap_while_it_fits(self):
        thread = browser_thread.Thread(memory="V101: R$ 9.500", steps=[_entry("x"), _entry("y")])
        thread.fold(1, None)
        self.assertEqual(thread.memory, "V101: R$ 9.500\n" + browser_thread.omitted_note(1, 1))
        full = "m" * browser_protocol.MAX_MEMORY_BYTES
        thread = browser_thread.Thread(memory=full, steps=[_entry("x"), _entry("y")])
        thread.fold(1, None)
        self.assertEqual(thread.memory, full)

    def test_an_append_past_the_bound_drops_the_oldest_folded_lines(self):
        lines = browser_protocol.MAX_FOLDED_BYTES // browser_protocol.record_bytes(_record("n" * 300))
        thread = browser_thread.Thread(
            folded=[_record("n" * 300)] * lines, memory="m" * browser_protocol.MAX_MEMORY_BYTES
        )
        room = browser_protocol.MAX_HISTORY_BYTES - thread.history_bytes() - browser_protocol.entry_bytes(_entry(""))
        thread.append(_entry("x" * (room - 1_000)))
        thread.append(_entry("y" * 1_000))
        self.assertLessEqual(thread.history_bytes(), browser_protocol.MAX_HISTORY_BYTES)
        self.assertGreater(thread.folded_omitted, 0)
        self.assertEqual(len(thread.folded) + thread.folded_omitted, lines)

    def test_a_fold_keeps_the_folded_lines_within_their_bound(self):
        thread = browser_thread.Thread(folded=[_record("n" * 300)] * 20)
        thread.steps = [_entry("x")]
        thread.fold(1, "c" * 8_000)
        self.assertLessEqual(sum(map(browser_protocol.record_bytes, thread.folded)), browser_protocol.MAX_FOLDED_BYTES)
        self.assertGreater(thread.folded_omitted, 0)


if __name__ == "__main__":
    unittest.main()
