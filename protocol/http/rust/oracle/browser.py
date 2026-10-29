"""`browser`. A byte measure takes its canonical input: a record, a one-step thread's entry, or a thread."""

from __future__ import annotations

import browser

from . import urls
from .common import arguments, cases, group, text


def _authority(value):
    return browser.web_authority(value) if isinstance(value, str) else None


def _field_check(value):
    found = arguments(value, "name", "value")
    if found is None or not isinstance(found[0], str) or found[0] not in browser.ALL_FIELDS:
        return None
    return browser.FIELD_CHECKS[found[0]](found[1])


def _record_bytes(value):
    record = browser.canonical_record(value)
    return None if record is None else browser.record_bytes(record)


def _single(step):
    return {"folded": [], "folded_omitted": 0, "memory": "", "unnoted": 0, "steps": [step]}


def _entry_bytes(value):
    thread = browser.canonical_thread(_single(value))
    return None if thread is None else browser.entry_bytes(thread["steps"][0])


def _history_bytes(value):
    thread = browser.canonical_thread(value)
    return None if thread is None else browser.history_bytes(thread)


def _request_text_bytes(value):
    found = arguments(value, "objective", "thread", "observation", "downloads")
    if found is None or text(found[0]) is None or text(found[2]) is None:
        return None
    thread = browser.canonical_thread(found[1])
    return None if thread is None else browser.request_text_bytes(found[0], thread, found[2], found[3])


def _threads() -> list[object]:
    wait = {"step": {"kind": "wait"}, "outcome": "ok", "page": {"url": "about:blank", "title": ""}}
    empty = {"folded": [], "folded_omitted": 0, "memory": "", "unnoted": 0, "steps": []}
    lines = browser.MAX_FOLDED_BYTES // browser.record_bytes(wait)
    page_step = {**wait, "text": "a" * 100, "downloads": None, "screenshot": False}
    return [
        *cases("browser.thread"),
        {**empty, "folded": [wait] * lines, "memory": "a" * 200},
        {**empty, "folded": [wait] * (lines + 1)},
        {**empty, "steps": [page_step, page_step]},
    ]


def _entries() -> list[object]:
    return [step for thread in _threads() if isinstance(thread, dict) for step in thread.get("steps", [])]


def _records() -> list[object]:
    found = [record for thread in _threads() if isinstance(thread, dict) for record in thread.get("folded", [])]
    return found + [{key: entry.get(key) for key in ("step", "outcome", "page")} for entry in _entries()]


def _requests() -> list[object]:
    return [
        {"objective": "find it", "thread": thread, "observation": "page", "downloads": downloads}
        for thread in _threads()[:6]
        for downloads in (None, {"files": ["a"], "pending": 0, "truncated": False})
    ]


def _field_checks() -> list[object]:
    steps = [step for step in cases("browser.step") if isinstance(step, dict)]
    return [{"name": name, "value": step.get(name)} for step in steps for name in browser.ALL_FIELDS]


ENTRIES = {
    "browser.canonical_url": (browser.canonical_url, lambda: [*cases("browser.url"), *urls.corpus(2000)]),
    "browser.web_authority": (_authority, lambda: urls.corpus(2000)),
    "browser.canonical_objective": (browser.canonical_objective, lambda: cases("browser.objective")),
    "browser.canonical_step": (
        browser.canonical_step,
        lambda: [*cases("browser.step"), *[case["step"] for case in group("browser_blank_note")]],
    ),
    "browser.field_check": (_field_check, _field_checks),
    "browser.canonical_record": (browser.canonical_record, _records),
    "browser.canonical_thread": (browser.canonical_thread, _threads),
    "browser.canonical_memory": (browser.canonical_memory, lambda: ["note\nline", "", " ", "a\x07", "é" * 7000]),
    "browser.canonical_observation": (browser.canonical_observation, lambda: cases("browser.observation")),
    "browser.canonical_check": (browser.canonical_check, lambda: cases("browser.check")),
    "browser.canonical_downloads": (browser.canonical_downloads, lambda: cases("browser.downloads")),
    "browser.canonical_file_reference": (
        browser.canonical_file_reference,
        lambda: [
            item for case in cases("browser.terminal") if isinstance(case, dict) for item in case.get("files", [])
        ],
    ),
    "browser.canonical_terminal": (browser.canonical_terminal, lambda: cases("browser.terminal")),
    "browser.record_bytes": (_record_bytes, _records),
    "browser.entry_bytes": (_entry_bytes, _entries),
    "browser.history_bytes": (_history_bytes, _threads),
    "browser.request_text_bytes": (_request_text_bytes, _requests),
    "browser.STEP_SCHEMA": (lambda _value: browser.STEP_SCHEMA, lambda: [None]),
}
