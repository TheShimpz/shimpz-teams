"""Decode JSON without duplicate fields, non-finite numbers, lone surrogates, or nesting deeper than 512.

Bytes must be UTF-8 without a byte-order mark. The Rust crate `.standards/strict-json` implements the same admission,
proven by its vectors and the differential harness.
"""

from __future__ import annotations

import json
import math
import re

MAX_DEPTH = 512
_SURROGATE = re.compile("[\ud800-\udfff]")
_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")
_NOT_BRACKET = re.compile(r"[^\[\]{}]+")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_non_finite(_value):
    raise ValueError("non-finite JSON number")


def _finite_float(value):
    # An exponent overflow such as 1e999 decodes to infinity without passing through parse_constant.
    number = float(value)
    if not math.isfinite(number):
        _reject_non_finite(value)
    return number


def _check_depth(text):
    # Counts brackets outside strings, so the decoder never recurses deeper than MAX_DEPTH whatever the caller's stack.
    if text.count("[") + text.count("{") <= MAX_DEPTH:
        return
    depth = 0
    for bracket in _NOT_BRACKET.sub("", _STRING.sub("", text)):
        if bracket in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("JSON nesting is too deep")
        else:
            depth -= 1


def _check_surrogates(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                raise ValueError("lone surrogate in JSON string")
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def loads(data):
    if isinstance(data, (bytes, bytearray)):
        if data.startswith(b"\xef\xbb\xbf"):
            raise ValueError("JSON must not start with a byte-order mark")
        data = bytes(data).decode("utf-8")
    _check_depth(data)
    value = json.loads(
        data,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite,
        parse_float=_finite_float,
    )
    # A lone surrogate reaches a decoded string only through a \u escape or a str argument that already holds one.
    if _SURROGATE_ESCAPE.search(data) or _SURROGATE.search(data):
        _check_surrogates(value)
    return value
