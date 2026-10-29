r"""The pattern dialect of an Action schema's `pattern` values and `patternProperties` names.

An admitted pattern uses only syntax that RE2, Python `re`, and the Rust `regex` engine read alike: literals, escaped
punctuation, `\a`, `\f`, `\n`, `\r`, `\t`, `\v`, and `\xHH`; `.`, `^`, `$`, `\A`, `\b`, `\B`, and the ASCII Perl
classes; optionally negated bracketed classes of literals, ranges, and Perl classes, with an unescaped `-` only first or
last; alternation; greedy or lazy `*`, `+`, `?`, `{m}`, `{m,}`, and `{m,n}` applied to a literal, `.`, class, or group,
with every count at most 1,000, written without a leading zero, and nested counts multiplying to at most 1,000 on every
path; capturing, `(?P<name>)`, and non-capturing groups with scoped `i`, `m`, or `s` flags that may be negated; and
global `i`, `m`, or `s` flags only as one group at the very start. Its syntax tree nests at most 32 levels, counted as
the Rust `regex-syntax` parser counts them, and `pattern_program` bounds its RE2 program within 16,384 instructions.
That bound, the search overhead included, is also what one search charges per byte of its subject.

`pattern_preparation` is what preparing the pattern (reading its syntax and compiling its program) charges, once per
payload validation: a fixed PREPARATION_BASE for building a matcher, PREPARATION_WEIGHT per unit of the program bound,
and, for every bracketed class, the case-folding work the Rust engine spends on it under the `i` flag, which visits
every code point the class's members cover and is therefore charged by code point, not by program size.

This module reproduces, construct by construct, the Developers publication rule (`action-schema`'s `pattern.rs`, over
the `regex-syntax` 0.8 parse); the Assistant protocol's vectors and a seeded differential corpus pin the equality.
"""

from __future__ import annotations

MAX_PATTERN_NESTING = 32
MAX_REPEAT = 1000
MAX_PATTERN_PROGRAM = 16_384
# Instructions RE2 spends on an empty pattern and an unanchored search.
PROGRAM_OVERHEAD = 16
# What preparing any pattern charges for building its matcher, and what it charges per unit of the program bound:
# compiling costs far more per instruction than a search does per subject byte.
PREPARATION_BASE = 1 << 16
PREPARATION_WEIGHT = 256
# Case folding a class visits every code point its members cover, a literal as one and a Perl class as the ASCII range
# or, negated, every code point, whatever the flags. FOLD_WEIGHT is charged per code point below CASED_END, where
# every case mapping lies, and SCAN_WEIGHT per code point at or above it, which the fold only scans.
FOLD_WEIGHT = 64
SCAN_WEIGHT = 4
CASED_END = 0x20000
MAX_CODE_POINT = 0x10FFFF

_META = frozenset("\\.+*?()|[]{}^$#&-~")
_SPECIAL = {"a": "\x07", "f": "\x0c", "t": "\t", "n": "\n", "r": "\r", "v": "\x0b"}
_PERL = frozenset("dswDSW")
_FLAGS = frozenset("imsUuRx")
_ADMITTED_FLAGS = frozenset("ims")
_HEX = frozenset("0123456789abcdefABCDEF")
_DIGITS = frozenset("0123456789")
_IDENTIFIER_START = frozenset("_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_IDENTIFIER = _IDENTIFIER_START | _DIGITS
# The operands a repetition may apply to.
_REPEATABLE = frozenset({"literal", "dot", "perl", "class", "group"})


class _RefusedError(Exception):
    """The pattern is outside the admitted dialect."""


def pattern_costs(pattern: str) -> tuple[int, int] | None:
    """An admitted pattern's program bound, the search overhead included, and its preparation; None when refused."""
    try:
        # A lone surrogate is no Unicode text: neither RE2 nor the Rust engine can read it.
        pattern.encode()
        tree = _Parser(pattern).parse()
    except _RefusedError, UnicodeEncodeError:
        return None
    bound = PROGRAM_OVERHEAD + _bound(tree)
    if _depth(tree) > MAX_PATTERN_NESTING or _budget(tree, MAX_REPEAT) <= 0 or bound > MAX_PATTERN_PROGRAM:
        return None
    return bound, PREPARATION_BASE + PREPARATION_WEIGHT * bound + _folds(tree)


def pattern_program(pattern: str) -> int | None:
    """The upper bound of an admitted pattern's RE2 program, the search overhead included, or None when refused."""
    costs = pattern_costs(pattern)
    return None if costs is None else costs[0]


def pattern_preparation(pattern: str) -> int | None:
    """What preparing an admitted pattern charges against the matching budget, or None when refused."""
    costs = pattern_costs(pattern)
    return None if costs is None else costs[1]


def pattern_admitted(pattern: str) -> bool:
    """Whether a pattern is admitted: `pattern_program` bounds it."""
    return pattern_program(pattern) is not None


# The syntax tree, as `regex-syntax` shapes it: ("empty",), ("flags",), ("literal",), ("dot",), ("assertion",),
# ("perl", negated), ("class", ascii, members, folds), ("repeat", count, operand) with count None for `*`, `+`, and `?`,
# ("group", child), ("alternation", children), and ("concat", children) of two or more items.


class _Parser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.position = 0
        self.names: set[str] = set()

    def peek(self, offset: int = 0) -> str | None:
        index = self.position + offset
        return self.pattern[index] if index < len(self.pattern) else None

    def take(self) -> str:
        character = self.peek()
        if character is None:
            raise _RefusedError
        self.position += 1
        return character

    def parse(self) -> tuple:
        concat: list[tuple] = []
        # Each frame is ("alternation", branches) or ("group", outer concat, group kind).
        stack: list[tuple] = []
        while self.peek() is not None:
            character = self.peek()
            if character == "(":
                concat = self.open_group(concat, stack)
            elif character == ")":
                self.position += 1
                concat = self.close_group(concat, stack)
            elif character == "|":
                self.position += 1
                _alternate(concat, stack)
                concat = []
            elif character in "?*+":
                self.position += 1
                concat.append(self.repeat(concat, None))
            elif character == "{":
                concat.append(self.repeat(concat, self.counted()))
            elif character == "[":
                concat.append(self.bracketed())
            else:
                concat.append(self.primitive())
        tree = _close_alternation(concat, stack)
        if stack:
            raise _RefusedError
        return tree

    def open_group(self, concat: list[tuple], stack: list[tuple]) -> list[tuple]:
        start = self.position
        self.position += 1
        if self.pattern.startswith("?P<", self.position):
            self.position += 3
            self.capture_name()
        elif self.peek() == "?":
            self.position += 1
            flags, scoped = self.flags()
            if not scoped:
                # Global flags: only one group at the very start, setting flags without negation.
                if start != 0 or "-" in flags:
                    raise _RefusedError
                concat.append(("flags",))
                return concat
        stack.append(("group", concat))
        return []

    def close_group(self, concat: list[tuple], stack: list[tuple]) -> list[tuple]:
        inner = _close_alternation(concat, stack)
        if not stack:
            raise _RefusedError
        _, outer = stack.pop()
        outer.append(("group", inner))
        return outer

    def capture_name(self) -> None:
        end = self.pattern.find(">", self.position)
        name = self.pattern[self.position : end] if end >= 0 else ""
        if not name or name[0] not in _IDENTIFIER_START or not set(name) <= _IDENTIFIER or name in self.names:
            raise _RefusedError
        self.names.add(name)
        self.position = end + 1

    def flags(self) -> tuple[str, bool]:
        """The flags of `(?flags)` or `(?flags:`, and whether they are scoped; refused outside the admitted set."""
        flags = ""
        while self.peek() not in (":", ")"):
            character = self.take()
            if character not in _FLAGS and character != "-":
                raise _RefusedError
            if character in flags:
                raise _RefusedError
            flags += character
        scoped = self.take() == ":"
        if flags.endswith("-") or (not flags and not scoped) or not set(flags) <= _ADMITTED_FLAGS | {"-"}:
            raise _RefusedError
        return flags, scoped

    def counted(self) -> int:
        """The count of `{m}`, `{m,}`, or `{m,n}`, written without whitespace: the upper bound, or the lower one."""
        self.position += 1
        low = self.decimal()
        if self.peek() == ",":
            self.position += 1
            high = self.decimal() if self.peek() != "}" else None
            if high is not None and high < low:
                raise _RefusedError
        else:
            high = low
        if self.take() != "}":
            raise _RefusedError
        return low if high is None else high

    def decimal(self) -> int:
        start = self.position
        while self.peek() is not None and self.peek() in _DIGITS:
            self.position += 1
        digits = self.pattern[start : self.position]
        # RE2 reads a count with a leading zero as literal text, where Rust and Python read its value.
        if not digits or int(digits) >= 1 << 32 or (len(digits) > 1 and digits[0] == "0"):
            raise _RefusedError
        return int(digits)

    def repeat(self, concat: list[tuple], count: int | None) -> tuple:
        if not concat or concat[-1][0] not in _REPEATABLE or (count is not None and count > MAX_REPEAT):
            raise _RefusedError
        if self.peek() == "?":
            self.position += 1
        return ("repeat", count, concat.pop())

    def primitive(self) -> tuple:
        character = self.take()
        if character == "\\":
            return self.escape(in_class=False)
        if character == ".":
            return ("dot",)
        if character in "^$":
            return ("assertion",)
        return ("literal", character)

    def escape(self, *, in_class: bool) -> tuple:
        character = self.take()
        if character in _PERL:
            return ("perl", character.isupper())
        if character == "x":
            digits = self.pattern[self.position : self.position + 2]
            if len(digits) != 2 or not set(digits) <= _HEX:
                raise _RefusedError
            self.position += 2
            return ("literal", chr(int(digits, 16)))
        if character in _META or (character.isascii() and not character.isalnum() and character not in "<>"):
            return ("literal", character)
        if character in _SPECIAL:
            return ("literal", _SPECIAL[character])
        # Outside a class `\A`, `\b`, and `\B` assert; `\b{` would read a special boundary or repeat an assertion.
        if not in_class and (character == "A" or (character in "bB" and self.peek() != "{")):
            return ("assertion",)
        raise _RefusedError

    def bracketed(self) -> tuple:
        self.position += 1
        if self.peek() == "^":
            self.position += 1
        members: list[tuple] = []
        while self.peek() == "-":
            self.position += 1
            members.append(("literal", "-", True))
        if not members and self.peek() == "]":
            self.position += 1
            members.append(("literal", "]", False))
        while (character := self.take()) != "]":
            if character == "[" or (character in "&-~" and self.peek() == character):
                raise _RefusedError
            self.position -= 1
            members.append(self.class_member())
        return _class(members)

    def class_member(self) -> tuple:
        first = self.class_item()
        if self.peek() is None:
            raise _RefusedError
        if self.peek() != "-" or self.peek(1) in ("]", "-"):
            return first
        self.position += 1
        last = self.class_item()
        if first[0] != "literal" or last[0] != "literal" or first[1] > last[1]:
            raise _RefusedError
        return ("range", first[1], last[1])

    def class_item(self) -> tuple:
        character = self.take()
        if character != "\\":
            return ("literal", character, character == "-")
        item = self.escape(in_class=True)
        return item if item[0] == "perl" else (*item, False)


def _alternate(concat: list[tuple], stack: list[tuple]) -> None:
    if stack and stack[-1][0] == "alternation":
        stack[-1][1].append(_concat(concat))
    else:
        stack.append(("alternation", [_concat(concat)]))


def _close_alternation(concat: list[tuple], stack: list[tuple]) -> tuple:
    if stack and stack[-1][0] == "alternation":
        _, branches = stack.pop()
        return ("alternation", [*branches, _concat(concat)])
    return _concat(concat)


def _concat(items: list[tuple]) -> tuple:
    if not items:
        return ("empty",)
    return items[0] if len(items) == 1 else ("concat", items)


def _class(members: list[tuple]) -> tuple:
    dashes = [index for index, member in enumerate(members) if member[0] == "literal" and member[2]]
    if len(dashes) > 1 or (dashes and dashes[0] not in (0, len(members) - 1)):
        raise _RefusedError
    ascii_only = all(
        (member[0] == "literal" and member[1].isascii())
        or (member[0] == "range" and member[2].isascii())
        or (member[0] == "perl" and not member[1])
        for member in members
    )
    return ("class", ascii_only, len(members), sum(_member_folds(member) for member in members))


def _member_folds(member: tuple) -> int:
    """The folding work of one class member: the code points it covers, weighted by FOLD_WEIGHT or SCAN_WEIGHT."""
    if member[0] == "perl":
        low, high = 0, MAX_CODE_POINT if member[1] else 0x7F
    else:
        low, high = ord(member[1]), ord(member[2] if member[0] == "range" else member[1])
    cased = max(0, min(high, CASED_END - 1) - low + 1)
    return FOLD_WEIGHT * cased + SCAN_WEIGHT * (high - low + 1 - cased)


def _depth(node: tuple) -> int:
    """The nesting `regex-syntax` counts: each class, repetition, group, alternation, and concatenation.

    A class with two or more members nests its union one level further.
    """
    kind = node[0]
    if kind == "class":
        return 2 if node[2] > 1 else 1
    if kind in ("repeat", "group"):
        return 1 + _depth(node[-1])
    if kind in ("alternation", "concat"):
        return 1 + max(_depth(child) for child in node[1])
    return 0


def _budget(node: tuple, budget: int) -> int:
    """RE2's repetition walk: each counted repetition divides the remaining budget by its count."""
    kind = node[0]
    if kind == "repeat":
        count = node[1]
        return _budget(node[2], budget // count if count else budget)
    if kind == "group":
        return _budget(node[1], budget)
    if kind in ("alternation", "concat"):
        return min([budget, *(_budget(child, budget) for child in node[1])])
    return budget


def _folds(node: tuple) -> int:
    """The folding work of every bracketed class in the tree, once each however often a repetition copies it."""
    kind = node[0]
    if kind == "class":
        return node[3]
    if kind in ("repeat", "group"):
        return _folds(node[-1])
    if kind in ("alternation", "concat"):
        return sum(_folds(child) for child in node[1])
    return 0


_LEAF_BOUNDS = {"empty": 1, "flags": 0, "literal": 8, "dot": 16, "assertion": 2, "perl": 32}


def _bound(node: tuple) -> int:
    """An upper bound of the RE2 instructions one node compiles to under any flags (see the module docstring)."""
    kind = node[0]
    if kind in _LEAF_BOUNDS:
        return _LEAF_BOUNDS[kind]
    if kind == "class":
        return 32 + 4 * node[2] if node[1] else 128 + 32 * node[2]
    if kind == "repeat":
        operand = _bound(node[2])
        return operand + 2 if node[1] is None else max(node[1], 1) * (operand + 1) + 1
    if kind == "group":
        return _bound(node[1]) + 2
    if kind == "alternation":
        return len(node[1]) + sum(_bound(child) for child in node[1])
    return sum(_bound(child) for child in node[1])
