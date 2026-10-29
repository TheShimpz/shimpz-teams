"""`routine`."""

from __future__ import annotations

import routine

from .common import arguments, cases, group, small


def _daily_cap(value):
    schedule = routine.canonical_schedule(value)
    return None if schedule is None else routine.daily_cap(schedule)


def _continuous_cap(value):
    gap = small(value)
    return routine.continuous_cap(gap) if gap is not None and gap >= 1 else None


def _zoned(value):
    found = arguments(value, "timezone", "source")
    return None if found is None else routine.zoned(*found)


def _position(value):
    found = arguments(value, "value", "steps")
    return None if found is None else routine.canonical_position(*found)


def _step(value):
    found = arguments(value, "value", "position")
    if found is None or small(found[1]) is None:
        return None
    return routine.canonical_step(found[0], found[1])


def _disposition(value):
    found = arguments(value, "value", "total")
    return None if found is None else routine.canonical_disposition(*found)


def _where_text(value):
    return routine.where_text(value) if isinstance(value, str) or type(value) is int else None


def _pages() -> list[object]:
    pages = cases("routine_views.page")
    page = next(case for case in group("routine_views.page.valid") if case["next"] is None)
    step = page["steps"][0]
    big = {
        **step,
        "inputs": [{"member": f"m{index:02d}", "source": "literal", "value": "x" * 120} for index in range(40)],
    }
    total = page["offset"] + len(page["steps"])
    variants = [
        {**page, "routine_id": "x"},
        {**page, "plan_digest": "sha256:x"},
        {**page, "revision": 0},
        {**page, "total": 0},
        {**page, "offset": -1},
        {**page, "offset": total},
        {**page, "steps": "x"},
        {**page, "steps": []},
        {**page, "total": total + 1},
        {**page, "total": total + 1, "next": total},
        {**page, "total": total + 1, "next": float(total)},
        {**page, "total": total + 1, "next": None},
        {**page, "next": 0},
        {**page, "offset": page["offset"] + 1},
        {**page, "steps": [], "next": page["offset"]},
        {**page, "total": page["offset"] + 1, "next": None},
        {**page, "offset": 0, "total": 70, "steps": [step] * 65, "next": 65},
        {
            **page,
            "offset": 0,
            "total": 6,
            "steps": [{**big, "position": index + 1} for index in range(6)],
            "next": None,
        },
    ]
    wide = next(case for case in group("routine_views.page.valid") if case["offset"] == 0 and len(case["steps"]) >= 2)
    moved = [{**step, "position": index + 2} for index, step in enumerate(wide["steps"][:2])]
    variants.append({**wide, "offset": 1, "total": 2, "steps": moved, "next": 3})
    return [*pages, *variants]


def _step_variants(case: dict[str, object]) -> list[object]:
    step, at = case["value"], case["position"]
    literal = {"member": "a", "source": "literal", "value": "x"}
    reference = {"member": "b", "source": "step_output", "step": 1, "pointer": "/a", "where": None, "item": None}
    changes = [
        {"position": at + 1},
        {"position": float(at)},
        {"assistant": "A"},
        {"action": "A"},
        {"read_only": 1},
        {"inputs": [literal] * 2},
        {"inputs": [{**literal, "member": f"m{index:02d}"} for index in range(65)]},
        {"inputs": [{**literal, "member": "\x85"}]},
        {"inputs": [{**literal, "value": "\x85"}]},
        {"inputs": [{**literal, "source": "other"}]},
        {"inputs": [{**literal, "extra": 1}]},
        {"inputs": [reference]},
        {"inputs": [{**reference, "step": at}]},
        {"inputs": [{**reference, "pointer": "/" + "a" * 256}]},
        {"inputs": [{**reference, "pointer": "a"}]},
        {"inputs": [{**reference, "item": "/a"}]},
        {"inputs": [{**reference, "where": {"member": "m", "value_json": "1"}, "item": "/a"}]},
        {"inputs": [{**reference, "where": {"member": "m", "value_json": "1"}, "item": None}]},
        {"inputs": [{**reference, "where": {"member": "\x85", "value_json": "1"}, "item": "/a"}]},
        {"inputs": [{**reference, "where": {"member": "m"}, "item": "/a"}]},
        {"stored_inputs": "x"},
        {"stored_inputs": ["b", "a"]},
        {"stored_inputs": ["a", "a"]},
        {"stored_inputs": ["A"]},
        {"stored_inputs": [f"s{index}" for index in range(9)]},
        {"inputs": [{**literal, "member": f"m{index:02d}", "value": "x" * 120} for index in range(64)]},
    ]
    variants = [{"value": {**step, **change}, "position": at} for change in changes]
    return [*variants, {"value": step, "position": 0}, {"value": step, "position": 300}]


def _summaries() -> list[object]:
    summary = group("routine_views.summary.valid")[0]
    runs = summary["actions"]
    first = runs[0]
    variants = [
        {**summary, "actions": [[first[0], "A", first[2]], *runs[1:]]},
        {**summary, "actions": [first, first], "steps": first[2] * 2},
        {**summary, "actions": [first, ["other-assistant", first[1], 1]], "steps": first[2] + 1, "more": 0},
        {**summary, "more": -1},
        {**summary, "more": 1, "steps": summary["steps"] + 1},
        {**summary, "more": 2**64},
        {**summary, "steps": summary["steps"] + 1},
        {**summary, "steps": 0},
        {**summary, "actions": [[first[0], first[1]]]},
        {**summary, "actions": [[first[0], first[1], 0]]},
    ]
    return [*cases("routine_views.summary"), *variants]


SELECTOR_TEXTS = (
    '"\\ud800\\ud800"',
    '"\\ud800\\udc00"',
    '"\\ud800\\u0085"',
    '"\\udbff\\udfff"',
    '"\\udc00\\ud800"',
    '"\\ud800xxxxxxxxx"',
    '"\x85"',
    '"\\""',
    '"\\\\"',
    '"\\/"',
    '"\\b\\f\\n\\r\\t"',
    '"\\x"',
    '"\\ud800x"',
    '"\\ud800\\u0041"',
    '"\\ud800\\uzzzz"',
    '"\\uzzzz"',
    '"\\u00"',
    '"a',
    "-",
    "-12",
    "0",
    "1.5",
    "true",
    '"a"x',
    '"\\ud800"',
    '"\\ud83d\\ude00"',
    '"\\ud83d"',
    '"\\u0085"',
    '"\\u00e9"',
    "-0",
    "12",
    "012",
    '"a" ',
)


def _steps() -> list[object]:
    found = []
    for page in group("routine_views.page.valid"):
        for index, step in enumerate(page["steps"]):
            found.append({"value": step, "position": page["offset"] + index + 1})
    variants = []
    for case in found:
        for number, item in enumerate(case["value"]["inputs"]):
            if not isinstance(item.get("where"), dict):
                continue
            for text in SELECTOR_TEXTS:
                inputs = [*case["value"]["inputs"]]
                inputs[number] = {**item, "where": {**item["where"], "value_json": text}}
                variants.append({**case, "value": {**case["value"], "inputs": inputs}})
    return [*found, *variants, *(change for case in found for change in _step_variants(case))]


def _texts() -> list[object]:
    return [
        "",
        "a",
        "\u200b\x85\u00e9",
        "x" * 130,
        "a\u2028",
        "a\u2029",
        "a\u2028b",
        "a\u2029b",
        "a\u0301",
        *(case for case in cases("routine_timezone") if isinstance(case, str)),
    ]


def _outputs() -> list[object]:
    text = {"kind": "text", "value": "a", "cut": False}
    deepest = {"kind": "list", "items": [], "omitted": 0}
    for _ in range(4):
        deepest = {"kind": "list", "items": [deepest], "omitted": 0}
    nodes = [
        {"kind": "null"},
        {"kind": "null", "x": 1},
        {"kind": "bool", "value": True},
        {"kind": "bool", "value": 1},
        {"kind": "bool"},
        {"kind": "number", "value": "1.5e3"},
        {"kind": "number", "value": "1" * 65},
        {"kind": "number", "value": "01"},
        {"kind": "number"},
        text,
        {"kind": "text", "value": "a\x85", "cut": False},
        {"kind": "text", "value": "a" * 301, "cut": False},
        {"kind": "text", "value": "a"},
        {"kind": "other"},
        [],
        {"kind": "list", "items": [text], "omitted": 0},
        {"kind": "list", "items": [text], "omitted": 2**31},
        {"kind": "list", "items": [text], "omitted": 0, "x": 1},
        {"kind": "list", "items": [text] * 51, "omitted": 0},
        {"kind": "list", "items": {}, "omitted": 0},
        {"kind": "fields", "fields": [["a", text], ["b", text]], "omitted": 0},
        {"kind": "fields", "fields": [["a", text], ["a", text]], "omitted": 0},
        {"kind": "fields", "fields": [["a", text]] * 25, "omitted": 0},
        {"kind": "fields", "fields": [["a", text, 1]], "omitted": 0},
        {"kind": "fields", "fields": [["\x85", text]], "omitted": 0},
        {"kind": "fields", "fields": [["a", {"kind": "x"}]], "omitted": 0},
        {"kind": "fields", "fields": {}, "omitted": 0},
        {"kind": "fields", "fields": [], "omitted": 0, "x": 1},
        deepest,
    ]
    shown = [{"step": 1, "state": "shown", "value": node, "truncated": False} for node in nodes]
    return [*cases("routine_views.output"), *shown]


ENTRIES = {
    "routine.canonical_schedule": (routine.canonical_schedule, lambda: cases("routine_schedule")),
    "routine.exact_schedule": (routine.exact_schedule, lambda: cases("routine_schedule")),
    "routine.daily_cap": (_daily_cap, lambda: cases("routine_schedule")),
    "routine.continuous_cap": (_continuous_cap, lambda: [1, 5, 7, 3600, 86_401, 31_622_400]),
    "routine.canonical_name": (routine.canonical_name, _texts),
    "routine.canonical_description": (routine.canonical_description, _texts),
    "routine.exact_name": (routine.exact_name, _texts),
    "routine.exact_description": (routine.exact_description, _texts),
    "routine.canonical_timezone": (routine.canonical_timezone, lambda: cases("routine_timezone")),
    "routine.zoned": (
        _zoned,
        lambda: [
            {"timezone": zone, "source": source}
            for zone in ("UTC", "America/Sao_Paulo", "utc", None)
            for source in ("browser", "person", "none", "other")
        ],
    ),
    "routine.canonical_position": (
        _position,
        lambda: [*group("routine_position.valid"), *group("routine_position.invalid")],
    ),
    "routine.escaped": (lambda value: routine.escaped(value) if isinstance(value, str) else None, _texts),
    "routine.literal_preview": (routine.literal_preview, lambda: [*_texts(), *_pages()]),
    "routine.preview": (lambda value: routine.preview(value) if isinstance(value, str) else None, _texts),
    "routine.where_text": (_where_text, lambda: ["a", "\x85", 12, -3, *_texts()]),
    "routine.canonical_step": (_step, _steps),
    "routine.canonical_page": (routine.canonical_page, _pages),
    "routine.canonical_summary": (routine.canonical_summary, _summaries),
    "routine.canonical_disposition": (
        _disposition,
        lambda: [
            {"value": {"mode": mode, "step": step}, "total": total}
            for mode in ("show", "changes", "none")
            for step in (None, 1, 3)
            for total in (1, 3, 257)
        ],
    ),
    "routine.canonical_output": (routine.canonical_output, _outputs),
    "routine.encoded_bytes": (
        routine.encoded_bytes,
        lambda: [case["value"] for case in group("canonical_json.canonical")],
    ),
    "routine.valid_revision": (routine.valid_revision, lambda: [1, 0, 2**31 - 1, 2**31, True, 1.0, "1"]),
    "routine.canonical_run_usage": (routine.canonical_run_usage, lambda: cases("routine_run_usage")),
}
