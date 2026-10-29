"""A seeded corpus of origins and URLs for the URL validators of `websocket`, `browser`, and `sign_in` (tooling).

Each URL composes a scheme, a separator, userinfo, a host, a port, and a rest from pieces that reach every branch of
`urllib.parse`: case, ports with leading zeros, non-ASCII and out-of-range digits, IPv6 and IPvFuture brackets,
userinfo, non-ASCII hosts that lower or NFKC-normalize to ASCII or to delimiters, percent signs, backslashes, empty
queries and fragments, and the leading C0 controls, spaces, tabs, and newlines `urlsplit` strips or removes.
"""

from __future__ import annotations

from .mutate import SplitMix64

SEED = 20261029
SCHEMES = ("http", "https", "HTTP", "Https", "hTtPs", "ftp", "", "ht tp", "1http", "h", "httpſ", "httpsK")
SEPARATORS = ("://", "://", "://", ":/", ":", "//", ":///", ":\\\\")
USERINFO = ("", "", "", "", "user@", "user:pw@", "@", ":@", "a:b:c@", "u@v@", "%40@")
HOSTS = (
    "example.com",
    "EXAMPLE.Com",
    "www.linkedin.com",
    "localhost",
    "127.0.0.1",
    "[::1]",
    "[v1.x]",
    "[1.2.3.4]",
    "[::ffff:1.2.3.4]",
    "[fe80::1%eth0]",
    "[",
    "]",
    "[::1",
    "::1]",
    "münchen.de",
    "xn--mnchen-3ya.de",
    "Keep.com",
    "exa mple.com",
    "a℀b.com",
    "ａｂ.com",
    "a.b-c.co",
    "",
    "%41.com",
    "a_b.com",
    "sub.example.co.uk",
    "İstanbul.com",
    "a;b.com",
    "x.y",
    "admin.example",
)
PORTS = (
    "",
    "",
    "",
    ":",
    ":443",
    ":0443",
    ":8443",
    ":80",
    ":99999",
    ":abc",
    ":4 43",
    ":-1",
    ":١٢٣",
    ":443:1",
    ":00000000000000000000443",
)
RESTS = (
    "",
    "",
    "/",
    "/login",
    "/a?b=c",
    "?",
    "#",
    "?#",
    "/a b",
    "/é",
    "/a\\b",
    "\\",
    ";x",
    "/%",
    "/a?x[]=1",
    "#frag",
    "/\x07",
    "/ ",
)
PREFIXES = ("", "", "", "", " ", "\t", "\x00", "\x1f ")
INSERTIONS = ("\t", "\n", "\r", " ", "\x0b", "\x85", " ")


# The ordinary choice of each part, drawn most of the time so that a good share of the corpus is admitted.
ORDINARY = (
    ("https", "http", "HTTPS"),
    ("://",),
    ("",),
    ("example.com", "www.linkedin.com", "admin.example", "127.0.0.1", "localhost", "a.b-c.co"),
    ("", ":443", ":8443"),
    ("", "/", "/login", "/a?b=c", "#frag"),
    ("",),
)


def url(rng: SplitMix64) -> str:
    """One URL of the corpus."""
    parts = (SCHEMES, SEPARATORS, USERINFO, HOSTS, PORTS, RESTS, PREFIXES)
    chosen = [
        rng.pick(ordinary) if rng.below(10) < 7 else rng.pick(part)
        for part, ordinary in zip(parts, ORDINARY, strict=True)
    ]
    text = chosen[6] + "".join(chosen[:6])
    if text and rng.below(8) == 0:
        position = rng.below(len(text) + 1)
        text = text[:position] + rng.pick(INSERTIONS) + text[position:]
    return text


def corpus(count: int, seed: int = SEED) -> list[str]:
    """`count` URLs drawn from `seed`."""
    rng = SplitMix64(seed)
    return [url(rng) for _ in range(count)]
