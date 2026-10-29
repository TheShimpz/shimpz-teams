"""The seeded generic mutator of the differential harness: one random edit of a JSON value.

An edit replaces a random node with a scalar, or edits a string (inserting, deleting, or replacing characters from a
pool of Python whitespace, controls, format and separator characters, combining marks and non-NFC sequences, case
special cases, CJK, Arabic, astral characters, and URL and identifier punctuation; or growing it to a length around a
common bound), a number (around its bounds, or swapped for a boolean, a float, or a big integer), an array (dropping,
duplicating, swapping, or emptying items), or an object (dropping, adding, or renaming a member, or reversing their
order).
"""

from __future__ import annotations

import unicodedata

MASK = (1 << 64) - 1


class SplitMix64:
    """A seeded, non-cryptographic generator whose sequence never depends on the Python version."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
        return value ^ (value >> 31)

    def below(self, bound: int) -> int:
        return self.next() % bound

    def pick(self, options):
        return options[self.below(len(options))]


PIECES = (
    *"abcxyzAZ09_-./:@?#%&=+~ ",
    "\t",
    "\n",
    "\r",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1f",
    "\x85",
    "\xa0",
    "\u2028",
    "\u2029",
    "\u3000",
    "\u2009",
    "\x00",
    "\x07",
    "\x7f",
    "\x9f",
    "\u200b",
    "\u200d",
    "\ufeff",
    "\u202e",
    "\u2060",
    "\u00ad",
    "\u0301",
    "e\u0301",
    "\u00e9",
    "A\u030a",
    "\u212b",
    "\ufb01",
    "\u00df",
    "\u0130",
    "\u03a3",
    "\u03c2",
    "\u212a",
    "\u0436",
    "\u4e2d",
    "\u65e5\u672c",
    "\u0639",
    "\u0663",
    "\u06f3",
    "\uff11",
    "\u0967",
    "\U0001f600",
    "\U00010000",
    "\U000e0001",
    "\U0010ffff",
    "\ue000",
    "\u0378",
    "\u2010",
    "\u2014",
    " -",
    "- ",
    "://",
    "www.",
    "xn--",
    "https://",
    "http://",
    "\\",
    '"',
)
LENGTHS = (16, 20, 32, 40, 63, 64, 65, 80, 120, 128, 160, 240, 253, 255, 256, 280, 300, 500, 512, 600, 2048, 4096)
NUMBERS = (
    0,
    1,
    -1,
    2,
    3,
    5,
    7,
    12,
    23,
    24,
    28,
    29,
    59,
    60,
    199,
    200,
    255,
    256,
    257,
    511,
    512,
    513,
    599,
    600,
    2047,
    2048,
    2049,
    86_400_000,
    86_400_001,
    2**31 - 1,
    2**31,
    2**40 - 1,
    2**40,
    2**53,
    2**63 - 1,
    2**63,
    2**64,
    -(2**63),
    -(2**64),
    10**30,
    0.0,
    -0.0,
    1.0,
    1.5,
    2.0,
    256.0,
    1e16,
    1e300,
    -1e-300,
    True,
    False,
    None,
)
SCALARS = (None, True, False, 0, 1, "", "a", [], {}, 1.0, "\u00e9")
KEYS = ("type", "kind", "value", "id", "name", "text", "step", "steps", "valid", "x", "", "\u00e9")


def _edit_string(rng: SplitMix64, text: str) -> str:
    operation = rng.below(9)
    position = rng.below(len(text) + 1)
    piece = rng.pick(PIECES)
    length = rng.pick(LENGTHS) + rng.below(3) - 1
    edits = {
        3: lambda: text[:position] + text[position + 1 :],
        4: lambda: text[:position] + piece + text[position + 1 :],
        5: lambda: (text + (text[-1:] or piece) * length)[:length],
        6: lambda: rng.pick((str.upper, str.casefold, str.strip, lambda value: f" {value} "))(text),
        7: lambda: unicodedata.normalize(rng.pick(("NFC", "NFD", "NFKC")), text),
        8: lambda: text * 2,
    }
    return edits.get(operation, lambda: text[:position] + piece + text[position:])()


def _edit_number(rng: SplitMix64, number: object) -> object:
    if rng.below(2) and type(number) is int:
        return number + rng.pick((-2, -1, 1, 2, 10**6))
    if rng.below(2) and type(number) is int:
        return float(number) if abs(number) < 2**53 else True
    return rng.pick(NUMBERS)


def _edit_list(rng: SplitMix64, items: list) -> list:
    operation = rng.below(6)
    if not items or operation == 0:
        return [*items, mutate(rng, rng.pick(items) if items else rng.pick(SCALARS))]
    index = rng.below(len(items))
    if operation == 1:
        return items[:index] + items[index + 1 :]
    if operation == 2:
        return [*items[:index], items[index], *items[index:]]
    if operation == 3:
        other = rng.below(len(items))
        swapped = [*items]
        swapped[index], swapped[other] = swapped[other], swapped[index]
        return swapped
    if operation == 4:
        return []
    return [*items[:index], mutate(rng, items[index]), *items[index + 1 :]]


def _edit_dict(rng: SplitMix64, members: dict) -> dict:
    operation = rng.below(6)
    keys = [*members]
    if not keys or operation == 0:
        key = rng.pick(keys) + rng.pick(("", "_", "x")) if keys and rng.below(2) else rng.pick(KEYS)
        return {**members, key: rng.pick(SCALARS) if not keys else mutate(rng, members[rng.pick(keys)])}
    key = rng.pick(keys)
    if operation == 1:
        return {name: value for name, value in members.items() if name != key}
    if operation == 2:
        renamed = key.upper() if key != key.upper() else key + "_"
        return {(renamed if name == key else name): value for name, value in members.items()}
    if operation == 3:
        return dict(reversed(members.items()))
    return {**members, key: mutate(rng, members[key])}


def mutate(rng: SplitMix64, value: object) -> object:
    """One random edit somewhere in `value` (which is never changed in place)."""
    if rng.below(8) == 0:
        return rng.pick(SCALARS) if rng.below(2) else rng.pick(NUMBERS)
    if isinstance(value, str):
        return _edit_string(rng, value)
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return _edit_number(rng, value)
    if isinstance(value, list):
        return _edit_list(rng, value)
    return _edit_dict(rng, value)
