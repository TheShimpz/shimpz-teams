"""The two viewer credentials of a Browser a person may control (ADR-0117, Sign-ins amendment item 3).

Team draws them for each Browser, writes their hashes into its display (`browser-host credentials`), and injects the
matching one into the one upgrade request it sends for each stream, so a person's page never holds either. The display
is KasmVNC, whose password file holds SHA-256-crypt hashes under its fixed salt `kasm`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass

_SALT = b"kasm"
_ROUNDS = 5000
_ALPHABET = b"./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# The SHA-256-crypt output order: each triple of digest bytes becomes four characters.
_ORDER = (
    (0, 10, 20),
    (21, 1, 11),
    (12, 22, 2),
    (3, 13, 23),
    (24, 4, 14),
    (15, 25, 5),
    (6, 16, 26),
    (27, 7, 17),
    (18, 28, 8),
    (9, 19, 29),
)


def _encode(value: int, length: int) -> bytes:
    out = bytearray()
    for _ in range(length):
        out.append(_ALPHABET[value & 0x3F])
        value >>= 6
    return bytes(out)


def _repeated(digest: bytes, length: int) -> bytes:
    return digest * (length // 32) + digest[: length % 32]


def kasm_hash(password: str) -> str:
    """`$5$kasm$…`: the SHA-256-crypt (Drepper) hash of `password` under KasmVNC's salt, at the default 5000 rounds."""
    key = password.encode()
    alternate = hashlib.sha256(key + _SALT + key).digest()
    first = hashlib.sha256(key + _SALT + _repeated(alternate, len(key)))
    length = len(key)
    while length:
        first.update(alternate if length & 1 else key)
        length >>= 1
    digest = first.digest()
    p_bytes = _repeated(hashlib.sha256(key * len(key)).digest(), len(key))
    s_bytes = _repeated(hashlib.sha256(_SALT * (16 + digest[0])).digest(), len(_SALT))
    for round_ in range(_ROUNDS):
        step = hashlib.sha256(p_bytes if round_ & 1 else digest)
        if round_ % 3:
            step.update(s_bytes)
        if round_ % 7:
            step.update(p_bytes)
        step.update(digest if round_ & 1 else p_bytes)
        digest = step.digest()
    encoded = b"".join(_encode((digest[a] << 16) | (digest[b] << 8) | digest[c], 4) for a, b, c in _ORDER)
    encoded += _encode((digest[31] << 8) | digest[30], 3)
    return "$5$kasm$" + encoded.decode()


@dataclass(frozen=True, slots=True)
class Credentials:
    """One Browser's read-only and read-write viewer passwords, held only in Team's memory."""

    viewer: str
    controller: str

    @classmethod
    def draw(cls) -> Credentials:
        return cls(secrets.token_hex(24), secrets.token_hex(24))

    def file(self) -> bytes:
        """The display's password file: the read-only `viewer`, then the read-write `controller`."""
        return f"viewer:{kasm_hash(self.viewer)}:r\ncontroller:{kasm_hash(self.controller)}:rw\n".encode()

    def authorization(self, control: bool) -> str:
        """The Basic credential the upgrade of one stream carries: the controller's only for the person in control."""
        user, password = ("controller", self.controller) if control else ("viewer", self.viewer)
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
