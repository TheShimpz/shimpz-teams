"""Generate vectors.json from the Python oracle `.standards/strict_json.py` (tooling; run with `python3 -I`).

Each case is {"input_b64", "accept", "value"}: `value` is the canonical representation of the decoded value as
compact JSON text (null when refused). It is text, not a nested tree, so a case nested to the depth limit stays
readable by the strict decoder itself. `differential.py` imports the corpus generator from this file.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = 20261029
RANDOM_CASES = 600
MASK = (1 << 64) - 1


def load_oracle():
    spec = importlib.util.spec_from_file_location("strict_json_oracle", ROOT.parent / "strict_json.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORACLE = load_oracle()


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

    def chance(self, percent: int) -> bool:
        return self.below(100) < percent

    def pick(self, options):
        return options[self.below(len(options))]


def to_tree(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return {"int": str(value)}
    if isinstance(value, float):
        return {"float_bits": struct.pack(">d", value).hex()}
    if isinstance(value, list):
        return [to_tree(item) for item in value]
    return {"object": [[key, to_tree(item)] for key, item in value.items()]}


def decide(data: bytes) -> str | None:
    """Return the canonical representation of the oracle's value, or None when the oracle refuses the bytes."""
    try:
        value = ORACLE.loads(data)
    except ValueError, RecursionError:
        return None
    return json.dumps(to_tree(value), ensure_ascii=False, separators=(",", ":"))


# ── corpus generator ────────────────────────────────────────────────────────────────────────────────────────────────
WHITESPACE = (" ", "\t", "\n", "\r")
CHARACTERS = (
    *"abcxyzABZ019 _-",
    '"',
    "\\",
    "/",
    "[",
    "{",
    "\x00",
    "\x08",
    "\x0c",
    "\n",
    "\r",
    "\t",
    "\x1f",
    "\x7f",
    "\x80",
    "\N{LATIN SMALL LETTER E WITH ACUTE}",
    "\N{EURO SIGN}",
    "\N{LINE SEPARATOR}",
    "\N{ZERO WIDTH NO-BREAK SPACE}",
    "\N{REPLACEMENT CHARACTER}",
    chr(0xFFFF),
    "\N{GRINNING FACE}",
    "\U0010ffff",
)
SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "/": "\\/", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
KEYS = ("a", "b", "id", "k", "A", "\N{LATIN SMALL LETTER E WITH ACUTE}", "\x00", "")
TOKENS = (
    b'"',
    b"\\",
    b"{",
    b"}",
    b"[",
    b"]",
    b",",
    b":",
    b"-",
    b"+",
    b"0",
    b"1",
    b".",
    b"e",
    b"E",
    b"u",
    b" ",
    b"\t",
    b"\x0b",
    b"\x00",
    b"\x1f",
    b"\x7f",
    b"\x80",
    b"\xc3",
    b"\xc3\xa9",
    b"\xed\xa0\x80",
    b"\xef\xbb\xbf",
    b"\xf4\x90\x80\x80",
    b"\xff",
    b"\\ud800",
    b"\\udc00",
    b"\\uD83D\\uDE00",
    b"\\u0000",
    b"\\x",
    b"NaN",
    b"Infinity",
    b"true",
    b"null",
    b"1e999",
    b'"a":1',
)


def space(rng: SplitMix64) -> str:
    return "".join(rng.pick(WHITESPACE) for _ in range(rng.pick((0, 0, 0, 1, 2))))


def digit_run(rng: SplitMix64, longest: int) -> str:
    return "".join(rng.pick("0123456789") for _ in range(1 + rng.below(longest)))


def number(rng: SplitMix64) -> str:
    sign = rng.pick(("", "", "-"))
    integral = "0" if rng.chance(20) else rng.pick("123456789") + digit_run(rng, rng.pick((3, 18, 40)))[1:]
    fraction = "." + digit_run(rng, 20) if rng.chance(35) else ""
    exponent = ""
    if rng.chance(30):
        exponent = rng.pick("eE") + rng.pick(("", "+", "-")) + rng.pick((digit_run(rng, 3), "308", "309", "400"))
    return sign + integral + fraction + exponent


def unicode_escape(rng: SplitMix64, character: str) -> str:
    code = ord(character)
    units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + (code & 0x3FF)]
    form = rng.pick(("\\u{:04x}", "\\u{:04X}"))
    return "".join(form.format(unit) for unit in units)


def encode_character(rng: SplitMix64, character: str) -> str:
    must_escape = character in '"\\' or ord(character) < 0x20
    if character in SHORT_ESCAPES and (must_escape or rng.chance(30)) and rng.chance(70):
        return SHORT_ESCAPES[character]
    if must_escape or rng.chance(25):
        return unicode_escape(rng, character)
    return character


def string(rng: SplitMix64, text: str | None = None) -> str:
    if text is None:
        text = "".join(rng.pick(CHARACTERS) for _ in range(rng.below(8)))
    return '"' + "".join(encode_character(rng, character) for character in text) + '"'


def scalar(rng: SplitMix64) -> str:
    kind = rng.below(6)
    if kind == 0:
        return rng.pick(("true", "false", "null"))
    if kind < 3:
        return number(rng)
    return string(rng)


def container(rng: SplitMix64, depth: int) -> str:
    count = rng.below(5)
    if rng.chance(50):
        items = [value(rng, depth + 1) for _ in range(count)]
        return "[" + space(rng) + ("," + space(rng)).join(item + space(rng) for item in items) + "]"
    keys = [rng.pick(KEYS) for _ in range(count)]
    if rng.chance(85):
        keys = list(dict.fromkeys(keys))
    members = [string(rng, key) + space(rng) + ":" + space(rng) + value(rng, depth + 1) + space(rng) for key in keys]
    return "{" + space(rng) + ("," + space(rng)).join(members) + "}"


def value(rng: SplitMix64, depth: int = 0) -> str:
    if depth >= 4 or rng.chance(40):
        return scalar(rng)
    return container(rng, depth)


def deep(rng: SplitMix64) -> bytes:
    levels = 505 + rng.below(12)
    openers = [rng.pick(("[", '{"k":')) for _ in range(levels)]
    closers = ["]" if opener == "[" else "}" for opener in reversed(openers)]
    return ("".join(openers) + scalar(rng) + "".join(closers)).encode()


def mutate(rng: SplitMix64, data: bytes) -> bytes:
    for _ in range(1 + rng.below(3)):
        at = rng.below(len(data) + 1)
        operation = rng.below(5)
        if operation == 0:
            data = data[:at] + rng.pick(TOKENS) + data[at:]
        elif operation == 1:
            data = data[:at] + data[at + 1 + rng.below(4) :]
        elif operation == 2:
            data = data[:at] + bytes([rng.below(256)]) + data[at + 1 :]
        elif operation == 3:
            data = data[:at]
        else:
            data = data[:at] + data[at : at + 1 + rng.below(8)] + data[at:]
    return data


def document(rng: SplitMix64) -> bytes:
    """Return one seeded document: mostly valid, often mutated, sometimes nested to the limit or token soup."""
    roll = rng.below(100)
    if roll < 45:
        return (space(rng) + value(rng) + space(rng)).encode()
    if roll < 88:
        return mutate(rng, value(rng).encode())
    if roll < 93:
        return deep(rng)
    return b"".join(rng.pick(TOKENS) for _ in range(rng.below(10)))


# ── fixed cases ─────────────────────────────────────────────────────────────────────────────────────────────────────
def encoding_cases() -> list[bytes]:
    invalid = (b"\x80", b"\xc0\x80", b"\xc3", b"\xe0\x80\x80", b"\xed\xa0\x80", b"\xed\xbf\xbf", b"\xf4\x90\x80\x80")
    invalid += (b"\xf5\x80\x80\x80", b"\xfe", b"\xff", b"\xf0\x9f\x98")
    cases = [b"\xef\xbb\xbf1", b"\xef\xbb\xbf", b" \xef\xbb\xbf1", b'"\xef\xbb\xbf"', b"\xef\xbb", b"1\xef\xbb\xbf"]
    for sequence in invalid:
        cases += [sequence, b'"' + sequence + b'"', b"[1," + sequence + b"]", b'{"' + sequence + b'":1}']
    return cases


def whitespace_cases() -> list[bytes]:
    cases = [b"", b" ", b" \t\r\n", b" \t\r\n1\r\n\t ", b"[ 1 , 2 ]", b'{ "a" : 1 , "b" : 2 }', b"[\n]", b"{\r}"]
    for other in (b"\x0b", b"\x0c", b"\xc2\xa0", b"\xe2\x80\x8b", b"\xe3\x80\x80", b"\x00", b"\x1c"):
        cases += [other + b"1", b"1" + other, b"[1" + other + b"]"]
    return cases


def literal_cases() -> list[bytes]:
    words = (b"true", b"false", b"null", b"True", b"FALSE", b"Null", b"tru", b"fals", b"nul", b"t", b"truex", b"nulll")
    constants = (b"NaN", b"Infinity", b"-Infinity", b"nan", b"inf", b"-NaN", b"+Infinity", b"[NaN]", b'{"a":Infinity}')
    return [*words, *constants, b"[true,false,null]", b"[true false]"]


def number_cases() -> list[bytes]:
    texts = "0 -0 1 -1 01 -01 00 1. .1 1.0 -0.0 0.0 0e0 -0e5 1e5 1E5 1e+5 1E-5 1e 1e+ 1e- 1.e5 +1 --1 - -a 1x 0x10"
    texts += " 9223372036854775807 9223372036854775808 -9223372036854775808 -9223372036854775809"
    texts += " 18446744073709551616 1e308 1.7976931348623157e308 1.7976931348623158e308 1.7976931348623159e308"
    texts += " 1e309 -1e309 1E400 5e-324 2.4703282292062328e-324 2.4703282292062327e-324 2e-324 1e-400 -1e-400"
    texts += " 0e99999999999999999999 1e-99999999999999999999 1e99999999999999999999 0.1 0.30000000000000004 1_0"
    cases = [text.encode() for text in texts.split()]
    for digits in (4299, 4300, 4301):
        cases += [b"1" * digits, b"-" + b"1" * digits, b"[" + b"9" * digits + b"]"]
    cases += [b"1." + b"1" * 5000, b"0." + b"0" * 400 + b"1e400", b"1" + b"0" * 308 + b".0", b"1" + b"0" * 309 + b".0"]
    for text in ("\N{ARABIC-INDIC DIGIT THREE}", "\N{FULLWIDTH DIGIT ONE}", "1\N{ARABIC-INDIC DIGIT THREE}"):
        cases.append(text.encode())
    cases += [b"-\xc3\xa9", b"[1,-]", b"[-0,0.0,-0.0]"]
    return cases


def escape_cases() -> list[bytes]:
    texts = [
        r'"\"\\\/\b\f\n\r\t"',
        '"\\u0000\\u001f\\u0020\\u007f\\u00e9\\u20AC\\uFFFF\\uffff\\uFEFF"',
        r'"\x41"',
        r'"\U0041"',
        r'"\a"',
        r'"\0"',
        r'"\'"',
        r'"\ "',
        r'"\u12"',
        r'"\u12g4"',
        r'"\u"',
        r'"\\',
        '"\\uD7FF"',
        '"\\uD800"',
        '"\\uDBFF"',
        '"\\uDC00"',
        '"\\uDFFF"',
        '"\\uE000"',
        '"\\uD800\\uDC00"',
        '"\\uDBFF\\uDFFF"',
        '"\\ud83d\\ude00"',
        '"\\uD83D\\uDE00x"',
        '"\\uD800\\uD7FF"',
        '"\\uD800\\uE000"',
        '"\\uD800\\uDBFF"',
        '"\\uD800\\uD800\\uDC00"',
        '"\\uD800x\\uDC00"',
        '"\\uD800\\n"',
        '"\\uD800\\u"',
        '"\\uD800\\u12"',
        '"\\uD800\\',
        '"\\uDC00\\uD800"',
        '{"\\uD83D\\uDE00":1}',
        '{"\\uD800":1}',
        '"\\\\uD800"',
        '"\\\\\\uD800"',
        r'"\/\/"',
        '{"a":1,"\\u0061":2}',
        '{"\\uD83D\\uDE00":1,"\N{GRINNING FACE}":2}',
    ]
    cases = [text.encode() for text in texts]
    cases += [b'"\x00"', b'"a\x1fb"', b'"\x7f"', b'"\t"', b'"\n"', b'"\r"', b'"abc', b'"', b'"\\"', b"'a'"]
    cases += ['"\U0010ffff\N{LINE SEPARATOR}\N{REPLACEMENT CHARACTER}"'.encode(), b'"\xc3\xa9"']
    return cases


def structure_cases() -> list[bytes]:
    texts = (
        '[]|[ ]|[1,2]|[1,]|[,1]|[1 2]|[1,,2]|[[]]|]|[|[ [1]]|{}|{ }|{|}|{"a":1}|{"a":1,}|{"a" 1}|{"a":}|{1:1}'
        '|{\'a\':1}|{"a":1 "b":2}|{"a"}|{"a":1}}|{,}|{"a":1,"b":[{"c":null}]}|{"a":1,"a":1}'
        '|{"a":{"b":1,"b":2}}|{"a":{"b":1},"b":{"a":1}}|{"a":1,"A":2}|{"":1,"":2}|[{"a":1},{"a":1}]'
        '|{"x":[{"a":1,"a":2}]}|1|-1.5|"s"|null|"a"b|1,2|{}{}|[]]|null[]|1 2|{"a":1}x|[1]2|{"a":[}]'
    )
    return [text.encode() for text in texts.split("|")]


def depth_cases() -> list[bytes]:
    pairs = (("[", "]"), ('{"k":', "}"))
    cases = [(opener * n + "0" + closer * n).encode() for opener, closer in pairs for n in (511, 512, 513, 600)]
    deep = "[" * 511 + "]" * 511
    cases += [f"[{sibling},{sibling},{deep}]".encode() for sibling in ("[]", "{}", "[0]", '{"k":0}')]
    cases.append(("[" * 512 + '"[[[[{{{{"' + "]" * 512).encode())
    cases.append(('["' + "[" * 600 + '"]').encode())
    cases.append(("[" * 512 + "]" * 512 + " x").encode())
    cases.append(("[{}," * 300 + "0" + "]" * 300).encode())
    mixed = "".join(("[", '{"a":')[level % 2] for level in range(512))
    cases.append((mixed + "1" + "".join(("]", "}")[level % 2] for level in reversed(range(512)))).encode())
    return cases


def fixed_cases() -> list[bytes]:
    return [
        *encoding_cases(),
        *whitespace_cases(),
        *literal_cases(),
        *number_cases(),
        *escape_cases(),
        *structure_cases(),
        *depth_cases(),
    ]


def main() -> int:
    rng = SplitMix64(SEED)
    inputs = fixed_cases() + [document(rng) for _ in range(RANDOM_CASES)]
    cases = []
    for data in dict.fromkeys(inputs):
        decided = decide(data)
        cases.append({"input_b64": base64.b64encode(data).decode(), "accept": decided is not None, "value": decided})
    (ROOT / "vectors.json").write_text("[\n" + ",\n".join(json.dumps(case) for case in cases) + "\n]\n")
    accepted = sum(case["accept"] for case in cases)
    print(f"vectors.json: {len(cases)} cases, {accepted} accepted, {len(cases) - accepted} refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
