"""Decode JSON without duplicate fields or non-finite numbers."""

from __future__ import annotations

import json
import math


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


def loads(data):
    return json.loads(
        data,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite,
        parse_float=_finite_float,
    )
