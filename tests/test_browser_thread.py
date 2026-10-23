"""A Browser task's thread: folds aim at their target, and appends and trims keep every bound."""

import unittest

from browser import thread as browser_thread
from protocol.http.v1 import browser as browser_protocol


def _entry(text: str, note: str | None = None) -> dict[str, object]:
    step = {"kind": "wait"} if note is None else {"kind": "wait", "note": note}
    page = {"url": "https://example.com", "title": "Example"}
    return {"step": step, "outcome": "ok", "page": page, "text": text, "downloads": None, "screenshot": False}


class ThreadTests(unittest.TestCase):
    def test_a_fold_stops_once_the_history_reaches_its_target(self):
        thread = browser_thread.Thread(steps=[_entry("x" * 4_000) for _ in range(10)])
        count = thread.to_fold(9_600)
        self.assertGreater(count, 0)
        self.assertLess(count, len(thread.steps))

    def test_an_append_past_the_bound_drops_folded_lines_then_notes(self):
        record = {key: value for key, value in _entry("", "n" * 200).items() if key in ("step", "outcome", "page")}
        thread = browser_thread.Thread(folded=[record] * 3, notes=["a" * 8_000, "b" * 8_000])
        thread.append(_entry("x" * 20_000))
        thread.append(_entry("y" * 20_000))
        self.assertLessEqual(thread.history_bytes(), browser_protocol.MAX_HISTORY_BYTES)
        self.assertEqual((thread.folded, thread.folded_omitted), ([], 3))
        self.assertEqual(thread.notes_omitted, 2)

    def test_a_fold_keeps_the_folded_lines_and_notes_within_their_bounds(self):
        record = {key: value for key, value in _entry("", "n" * 300).items() if key in ("step", "outcome", "page")}
        thread = browser_thread.Thread(folded=[record] * 20, notes=["a" * 8_000, "b" * 8_000])
        thread.steps = [_entry("x")]
        thread.fold(1, "c" * 8_000)
        self.assertLessEqual(sum(map(browser_protocol.record_bytes, thread.folded)), browser_protocol.MAX_FOLDED_BYTES)
        self.assertLessEqual(sum(len(note.encode()) for note in thread.notes), browser_protocol.MAX_NOTES_BYTES)
        self.assertGreater(thread.folded_omitted, 0)
        self.assertEqual(thread.notes_omitted, 1)


if __name__ == "__main__":
    unittest.main()
