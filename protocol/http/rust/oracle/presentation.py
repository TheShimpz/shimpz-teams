"""`content`, `preview`, `browser_view`, `challenge`, and `supervisor`."""

from __future__ import annotations

import browser_view
import challenge
import content
import preview
import supervisor

from .common import arguments, cases, group, text

ERRORS = {supervisor.SupervisorAssertionError: "supervisor"}


def _accept(value):
    key = browser_view.canonical_key(value)
    return None if key is None else browser_view.accept(key)


def _rendered(value):
    found = arguments(value, "value", "request")
    return None if found is None else challenge.canonical_rendered(*found)


def _claims(call):
    def adapter(value):
        found = arguments(value, "value", "audience")
        if found is None or text(found[1]) is None:
            return None
        result = call(found[0], audience=found[1])
        return result.decode() if isinstance(result, bytes) else result

    return adapter


def _claim_seeds() -> list[object]:
    seeds = [{"value": case, "audience": supervisor.ASSERTION_AUDIENCE} for case in cases("local_supervisor")]
    seeds += [{"value": case, "audience": supervisor.ROUTINE_AUDIENCE} for case in cases("local_routine")]
    seeds += [{"value": case["claims"], "audience": case["audience"]} for case in group("canonical_json.claims_json")]
    return seeds


def _keys() -> list[object]:
    return [case["key"] for case in group("browser_view_accept")] + cases("browser_view_key")


ENTRIES = {
    "content.canonical_framing": (content.canonical_framing, lambda: cases("file_content")),
    "preview.canonical_framing": (preview.canonical_framing, lambda: cases("file_preview")),
    "preview.canonical_page": (preview.canonical_page, lambda: cases("file_page")),
    "preview.canonical_page_framing": (preview.canonical_page_framing, lambda: cases("file_page_preview")),
    "browser_view.canonical_view": (browser_view.canonical_view, lambda: cases("browser_view")),
    "browser_view.canonical_key": (browser_view.canonical_key, _keys),
    "browser_view.accept": (_accept, _keys),
    "challenge.canonical_rendered": (
        _rendered,
        lambda: [{"value": case["rendered"], "request": case["request"]} for case in cases("rendered_copy")],
    ),
    "challenge.canonical_file_disclosure": (challenge.canonical_file_disclosure, lambda: cases("file_disclosure")),
    "challenge.canonical_input_projection": (challenge.canonical_input_projection, lambda: cases("input_projection")),
    "supervisor.canonical_claims": (_claims(supervisor.canonical_claims), _claim_seeds),
    "supervisor.canonical_json": (
        lambda value: supervisor.canonical_json(value).decode(),
        lambda: (
            [case["value"] for case in _claim_seeds()] + [case["value"] for case in group("canonical_json.canonical")]
        ),
    ),
    "supervisor.claims_json": (_claims(supervisor.claims_json), _claim_seeds),
    "supervisor.canonical_key_rotation": (
        supervisor.canonical_key_rotation,
        lambda: cases("supervisor_key_rotation"),
    ),
}
