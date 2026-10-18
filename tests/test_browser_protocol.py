"""The Browser agent's closed step set: only canonical steps, objectives, histories, and observations pass."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from protocol.http.v1 import browser

PROTOCOL = Path(browser.__file__).resolve().parent


def _flat(**fields):
    return {"kind": fields.pop("kind"), **dict.fromkeys(browser.ALL_FIELDS), **fields}


class BrowserProtocolTests(unittest.TestCase):
    def test_each_kind_admits_exactly_its_fields_from_the_flat_or_compact_form(self):
        cases = [
            (
                _flat(kind="navigate", url="https://example.com/a?b=1"),
                {"kind": "navigate", "url": "https://example.com/a?b=1"},
            ),
            (_flat(kind="click", ref="p3:17"), {"kind": "click", "ref": "p3:17"}),
            (
                _flat(kind="type", ref="p1:0", text="inteligência artificial"),
                {"kind": "type", "ref": "p1:0", "text": "inteligência artificial"},
            ),
            (_flat(kind="type", ref="p1:0", text=""), {"kind": "type", "ref": "p1:0", "text": ""}),
            (_flat(kind="scroll", direction="down"), {"kind": "scroll", "direction": "down"}),
            (_flat(kind="wait"), {"kind": "wait"}),
            (_flat(kind="screenshot"), {"kind": "screenshot"}),
            (
                _flat(kind="done", summary="Prazo: 30/11.\nFonte: edital."),
                {"kind": "done", "summary": "Prazo: 30/11.\nFonte: edital."},
            ),
            (
                _flat(kind="needs_person", reason="sign-in", sentence="O site pede login."),
                {"kind": "needs_person", "reason": "sign-in", "sentence": "O site pede login."},
            ),
            ({"kind": "click", "ref": "p3:17"}, {"kind": "click", "ref": "p3:17"}),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(browser.canonical_step(raw), expected)

    def test_anything_outside_the_closed_set_is_refused(self):
        for raw in (
            None,
            [],
            {},
            {"kind": 3},
            _flat(kind="run_script"),
            _flat(kind="click", ref="p1:1", url="https://example.com"),
            {**_flat(kind="wait"), "extra": 1},
            _flat(kind="click", ref="#button"),
            _flat(kind="click"),
            _flat(kind="type", ref="p1:0", text="line\nbreak"),
            _flat(kind="type", ref="p1:0", text="x" * (browser.MAX_TEXT_CHARS + 1)),
            _flat(kind="scroll", direction="left"),
            _flat(kind="done", summary="   "),
            _flat(kind="done", summary="é" * 2100),
            _flat(kind="done", summary="bell\x07"),
            _flat(kind="needs_person", reason="other", sentence="x"),
            _flat(kind="needs_person", reason="captcha", sentence="two\nlines"),
            _flat(kind="navigate", url="javascript:alert(1)"),
            _flat(kind="navigate", url="file:///etc/passwd"),
            _flat(kind="navigate", url="https://user:secret@example.com/"),
            _flat(kind="navigate", url="https:///no-host"),
            _flat(kind="navigate", url="https://example.com/a b"),
            _flat(kind="navigate", url="https://[::1"),
            _flat(kind="navigate", url="https://example.com/\x00"),
            _flat(kind="navigate", url=7),
            _flat(kind="navigate", url="https://example.com/" + "a" * browser.MAX_URL_CHARS),
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(browser.canonical_step(raw))

    def test_any_step_may_carry_one_bounded_note_and_a_blank_one_is_none(self):
        kept = browser.canonical_step(_flat(kind="click", ref="p1:1", note="Latest: Python 3.15.0"))
        self.assertEqual(kept, {"kind": "click", "ref": "p1:1", "note": "Latest: Python 3.15.0"})
        for blank in (None, "", "   "):
            with self.subTest(note=blank):
                self.assertEqual(browser.canonical_step(_flat(kind="wait", note=blank)), {"kind": "wait"})
        for note in ("a\nb", "bell\x07", "x" * (browser.MAX_NOTE_CHARS + 1), 7):
            with self.subTest(note=note):
                self.assertIsNone(browser.canonical_step(_flat(kind="wait", note=note)))

    def test_the_flat_schema_names_every_field_and_kind(self):
        self.assertEqual(browser.STEP_SCHEMA["required"], ["kind", *browser.ALL_FIELDS, "note"])
        self.assertEqual(browser.STEP_SCHEMA["properties"]["kind"]["enum"], list(browser.FIELDS))
        self.assertIs(browser.STEP_SCHEMA["additionalProperties"], False)

    def test_objectives_histories_and_observations_are_bounded(self):
        self.assertEqual(browser.canonical_objective("  Veja o prazo do edital.\n"), "Veja o prazo do edital.")
        for objective in (None, "", "   ", "x" * (browser.MAX_OBJECTIVE_CHARS + 1), "bell\x07"):
            self.assertIsNone(browser.canonical_objective(objective))
        step = {"kind": "click", "ref": "p1:1", "note": "Prazo: 30/11."}
        page = {"url": "https://example.com/edital", "title": "Edital"}
        entry = {"step": step, "outcome": "ok", "page": page}
        self.assertEqual(browser.canonical_history([entry]), [entry])
        for history in (
            None,
            [entry] * (browser.MAX_HISTORY + 1),
            [{"step": step, "page": page}],
            [{"step": step, "outcome": "ok"}],
            [{**entry, "outcome": "maybe"}],
            [{**entry, "step": {"kind": "click"}}],
            [{**entry, "step": _flat(kind="click", ref="p1:1")}],
            [{**entry, "step": {"kind": "done", "summary": "x"}}],
            [{**entry, "page": {"url": "https://example.com"}}],
            [{**entry, "page": {**page, "title": "x" * (browser.MAX_TITLE_CHARS + 1)}}],
            [{**entry, "page": {**page, "url": 7}}],
            ["step"],
        ):
            with self.subTest(history=history):
                self.assertIsNone(browser.canonical_history(history))
        self.assertEqual(browser.canonical_observation("url: x"), "url: x")
        self.assertIsNone(browser.canonical_observation("x" * (browser.MAX_OBSERVATION_BYTES + 1)))
        self.assertIsNone(browser.canonical_observation(None))

    def test_a_terminal_names_kept_files_by_reference_and_losses_by_closed_reason(self):
        kept = {"id": "a" * 32, "name": "edital.pdf", "media_type": "application/pdf", "size": 9}
        lost = {"name": "Unconfirmed 1.crdownload", "reason": "incomplete"}
        terminal = {"files": [kept], "lost": [lost], "skipped": 1}
        self.assertEqual(browser.canonical_terminal(terminal), terminal)
        empty = {"files": [], "lost": [], "skipped": 0}
        self.assertEqual(browser.canonical_terminal(empty), empty)
        for value in (
            None,
            {"files": [], "lost": []},
            {"files": "x", "lost": [], "skipped": 0},
            {"files": [kept] * (browser.MAX_TERMINAL_FILES + 1), "lost": [], "skipped": 0},
            {"files": [], "lost": [lost] * (browser.MAX_TERMINAL_LOSSES + 1), "skipped": 0},
            {"files": [], "lost": [], "skipped": -1},
            {"files": [], "lost": [], "skipped": browser.MAX_TERMINAL_SKIPPED + 1},
            {"files": [{**kept, "id": "x"}], "lost": [], "skipped": 0},
            {"files": [{**kept, "size": 0}], "lost": [], "skipped": 0},
            {"files": [{**kept, "size": True}], "lost": [], "skipped": 0},
            {"files": [{**kept, "name": "a/b"}], "lost": [], "skipped": 0},
            {"files": [{**kept, "media_type": "PDF!"}], "lost": [], "skipped": 0},
            {"files": [{**kept, "extra": 1}], "lost": [], "skipped": 0},
            {"files": [], "lost": [{"name": "x", "reason": "other"}], "skipped": 0},
            {"files": [], "lost": [{"name": "", "reason": "empty"}], "skipped": 0},
            {"files": [], "lost": [{"name": "a\nb", "reason": "empty"}], "skipped": 0},
            {"files": [], "lost": [{"name": "é" * 200, "reason": "empty"}], "skipped": 0},
            {"files": [], "lost": ["x"], "skipped": 0},
        ):
            with self.subTest(value=str(value)[:80]):
                self.assertIsNone(browser.canonical_terminal(value))

    def test_the_module_also_loads_flat_beside_its_payload_as_the_brain_mirror_does(self):
        spec = importlib.util.spec_from_file_location("browser", PROTOCOL / "browser.py")
        flat = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", [str(PROTOCOL), *sys.path]), mock.patch.dict(sys.modules):
            sys.modules.pop("payload", None)
            spec.loader.exec_module(flat)
        self.assertEqual(flat.canonical_step({"kind": "wait"}), {"kind": "wait"})


if __name__ == "__main__":
    unittest.main()
