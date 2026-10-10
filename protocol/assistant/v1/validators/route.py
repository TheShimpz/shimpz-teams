"""Reference validation for a Stored Input's reviewed routes and Team's call-time match (Assistant Spec v1).

Every Stored Input declares ``routes``: 1 to 32 entries ``{"method", "path", "query"?}``, unique by method and path,
naming the only provider endpoints on its host that ever receive its value (ADR-0106 amendment). ``method`` is one of
``METHODS``. ``path`` is at most 512 characters of ``/``-prefixed segments, each a literal of 1 to 64 unreserved
characters other than ``.`` and ``..``, or ``*`` for exactly one concrete segment of 1 to 256 unreserved characters
other than ``.`` and ``..``. The optional ``query`` lists provider selectors that change which authority an endpoint
acts on, such as Meta's ``fields``: a call to that route carries each named parameter exactly once, with a raw value
that is one of the listed values.

A segment whose ASCII-lowercased text without ``-``, ``_``, ``.``, and ``~`` contains a ``CREDENTIAL_STEMS`` entry names
an endpoint that may issue, list, or exchange credentials. It is refused as a declared literal and, at call time, as
any concrete segment, so a wildcard never reaches one.

Team matches a call's request target as sent, path before query: a path with percent-encoding, an empty, dot, or
non-unreserved segment, or a trailing slash matches no route, so Team refuses rather than normalizes it.
"""

from __future__ import annotations

import re

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
MAX_ROUTES = 32
MAX_PATH = 512
MAX_SELECTORS = 8
MAX_SELECTOR_VALUES = 16
MAX_SELECTOR_VALUE = 256
WILDCARD = "*"
LITERAL = re.compile(r"[A-Za-z0-9._~-]{1,64}")
SEGMENT = re.compile(r"[A-Za-z0-9._~-]{1,256}")
SELECTOR_NAME = re.compile(r"[A-Za-z0-9._~-]{1,64}")
# A selector value is compared with the raw parameter value as sent, so a reserved character appears percent-encoded
# with uppercase hexadecimal digits, as standard query encoders write it.
SELECTOR_VALUE = re.compile(r"(?:[A-Za-z0-9._~-]|%[0-9A-F]{2})+")
CREDENTIAL_STEMS = ("apikey", "authoriz", "credential", "oauth", "password", "secret", "token")
_SEPARATORS = str.maketrans("", "", "-_.~")


def credential_segment(segment: str) -> bool:
    """Whether one path segment names an endpoint that may issue, list, or exchange credentials."""
    folded = segment.lower().translate(_SEPARATORS)
    return any(stem in folded for stem in CREDENTIAL_STEMS)


def routes_error(routes: object) -> str | None:
    """Return a stable reason when one Stored Input's declared routes are refused."""
    if not isinstance(routes, list) or not 1 <= len(routes) <= MAX_ROUTES:
        return "routes_invalid"
    declared: set[tuple[str, str]] = set()
    for route in routes:
        error = _route_error(route)
        if error is not None:
            return error
        if (route["method"], route["path"]) in declared:
            return "route_duplicate"
        declared.add((route["method"], route["path"]))
    return None


def call_error(routes: object, method: str, target: str) -> str | None:
    """Return a stable reason when Team refuses to send a credential with these routes on one call.

    ``target`` is the request target as sent: the path, then an optional ``?`` and raw query.
    """
    error = routes_error(routes)
    if error is not None:
        return error
    path, _, query = target.partition("?")
    segments = path[1:].split("/")
    if not path.startswith("/") or not all(_concrete(segment) for segment in segments):
        return "route_path"
    if not any(
        route["method"] == method and _matches(route["path"], segments) and _selected(route.get("query", []), query)
        for route in routes
    ):
        return "route"
    return None


def _route_error(route: object) -> str | None:
    if (
        not isinstance(route, dict)
        or not {"method", "path"} <= set(route) <= {"method", "path", "query"}
        or route["method"] not in METHODS
    ):
        return "route_invalid"
    path = route["path"]
    if not isinstance(path, str) or len(path) > MAX_PATH or not path.startswith("/"):
        return "route_path_invalid"
    segments = path[1:].split("/")
    if not all(segment == WILDCARD or _literal(segment) for segment in segments):
        return "route_path_invalid"
    if any(segment != WILDCARD and credential_segment(segment) for segment in segments):
        return "route_credential"
    if "query" in route and not _selectors_admitted(route["query"]):
        return "route_query_invalid"
    return None


def _literal(segment: str) -> bool:
    return LITERAL.fullmatch(segment) is not None and segment not in {".", ".."}


def _concrete(segment: str) -> bool:
    return SEGMENT.fullmatch(segment) is not None and segment not in {".", ".."} and not credential_segment(segment)


def _selectors_admitted(selectors: object) -> bool:
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= MAX_SELECTORS:
        return False
    names: set[str] = set()
    for selector in selectors:
        if (
            not isinstance(selector, dict)
            or set(selector) != {"name", "values"}
            or not isinstance(selector["name"], str)
            or SELECTOR_NAME.fullmatch(selector["name"]) is None
            or selector["name"].lower() in names
            or not _values_admitted(selector["values"])
        ):
            return False
        names.add(selector["name"].lower())
    return True


def _values_admitted(values: object) -> bool:
    return (
        isinstance(values, list)
        and 1 <= len(values) <= MAX_SELECTOR_VALUES
        and all(
            isinstance(value, str) and len(value) <= MAX_SELECTOR_VALUE and SELECTOR_VALUE.fullmatch(value) is not None
            for value in values
        )
        and len(set(values)) == len(values)
    )


def _matches(pattern: str, segments: list[str]) -> bool:
    expected = pattern[1:].split("/")
    return len(expected) == len(segments) and all(
        part in {WILDCARD, segment} for part, segment in zip(expected, segments, strict=True)
    )


def _selected(selectors: list[dict[str, object]], query: str) -> bool:
    """Each selector appears exactly once, compared without case, under its exact name with one listed raw value.

    A selected route's query has no ``;`` and only unreserved parameter names, so no encoded, case-varied, or
    alternately separated alias of a selector can reach a provider that would read it differently.
    """
    if not selectors:
        return True
    pairs = [part.partition("=") for part in query.split("&")] if query else []
    if ";" in query or any(SELECTOR_NAME.fullmatch(name) is None for name, _, _ in pairs):
        return False
    for selector in selectors:
        found = [(name, value) for name, _, value in pairs if name.lower() == str(selector["name"]).lower()]
        if len(found) != 1 or found[0][0] != selector["name"] or found[0][1] not in selector["values"]:
            return False
    return True
