"""A Sign-in's capsule as Team receives it (ADR-0117, Sign-ins amendment items 5 and 6).

`browser-host profile export` writes one ustar archive of Chromium's profile; Team checks it again as it arrives, by
the same rules the Browser imports it under: regular files and directories only, admissible paths, each entry once,
within the entry, file, and total bounds, and a well-formed end. It also keeps the profile's cookie store, the only
file it reads, to refuse a profile that holds a session at a known identity provider's sign-in host: the cookie
store names every host a cookie belongs to in plain text, so a profile that signed in there holds a cookie that host
receives. The store is untrusted: it is read in memory, defensively, within a bound of work, and only as Chromium's own
table; a capsule with no store, another store, or one that cannot be read so is held to name every such host.
"""

from __future__ import annotations

import sqlite3

BLOCK = 512
MAX_ENTRIES = 4096
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 16
MAX_PATH_BYTES = 255
COOKIES = "Default/Cookies"
# The most SQLite steps reading the cookie store may take, far beyond what a real store of 32 MiB needs.
MAX_SQL_STEPS = 50_000_000
_PROGRESS_EVERY = 10_000
# The sign-in hosts of the identity providers a profile must not carry a session for (partial coverage, stated).
IDENTITY_HOSTS = frozenset(
    {
        "accounts.google.com",
        "login.microsoftonline.com",
        "login.live.com",
        "appleid.apple.com",
        "idmsa.apple.com",
    }
)


class CapsuleError(ValueError):
    """The capsule is not one Team keeps."""


def _admissible(path: str) -> bool:
    parts = path.split("/")
    return (
        0 < len(path) <= MAX_PATH_BYTES
        and len(parts) <= MAX_DEPTH
        and all(part not in {"", ".", ".."} for part in parts)
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
    )


def _text(block: bytes, start: int, end: int) -> str:
    field = block[start:end].split(b"\0", 1)[0]
    try:
        return field.decode()
    except UnicodeDecodeError as exc:
        raise CapsuleError("a capsule header is not text") from exc


def _octal(block: bytes, start: int, end: int) -> int:
    text = _text(block, start, end).strip(" ")
    if not text or any(character not in "01234567" for character in text):
        raise CapsuleError("a capsule header holds no octal number")
    return int(text, 8)


def _header(block: bytes) -> tuple[str, bool, int]:
    """One header's path, whether it is a directory, and its size; anything but a file or a directory is refused."""
    if block[257:263] != b"ustar\0" or block[263:265] != b"00":
        raise CapsuleError("a capsule entry is not ustar")
    summed = block[:148] + b" " * 8 + block[156:]
    if _octal(block, 148, 156) != sum(summed):
        raise CapsuleError("a capsule header's checksum is wrong")
    prefix, name = _text(block, 345, 500), _text(block, 0, 100)
    path = (f"{prefix}/{name}" if prefix else name).rstrip("/")
    if not _admissible(path) or _text(block, 157, 257):
        raise CapsuleError("a capsule entry's path is not admissible")
    size = _octal(block, 124, 136)
    if block[156:157] == b"5" and size == 0:
        return path, True, 0
    if block[156:157] in {b"0", b"\0"} and size <= MAX_FILE_BYTES:
        return path, False, size
    raise CapsuleError("a capsule entry is neither a bounded file nor a directory")


class Check:
    """Check a capsule chunk by chunk, keeping only its cookie store; `finish` proves it ended well."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._seen: set[str] = set()
        self._directories: set[str] = {""}
        self._total = 0
        # The current file's remaining bytes and padding, and whether it is the cookie store.
        self._remaining = 0
        self._padding = 0
        self._keeping = False
        self._ended = 0
        self._done = False
        self.cookies = bytearray()

    def feed(self, chunk: bytes) -> None:
        if self._done and chunk:
            raise CapsuleError("the capsule has bytes after its end")
        self._pending.extend(chunk)
        while not self._done:
            if self._remaining:
                take = min(self._remaining, len(self._pending))
                if not take:
                    return
                if self._keeping:
                    self.cookies.extend(self._pending[:take])
                del self._pending[:take]
                self._remaining -= take
                continue
            if self._padding:
                take = min(self._padding, len(self._pending))
                if not take:
                    return
                if any(self._pending[:take]):
                    raise CapsuleError("a capsule file's padding is not zero")
                del self._pending[:take]
                self._padding -= take
                continue
            if len(self._pending) < BLOCK:
                return
            block = bytes(self._pending[:BLOCK])
            del self._pending[:BLOCK]
            self._block(block)

    def _block(self, block: bytes) -> None:
        if not any(block):
            self._ended += 1
            self._done = self._ended == 2
            return
        if self._ended:
            raise CapsuleError("the capsule's end is malformed")
        path, directory, size = _header(block)
        if path in self._seen:
            raise CapsuleError("the capsule names one path twice")
        if path.rpartition("/")[0] not in self._directories:
            raise CapsuleError("a capsule entry comes before its directory")
        if path.rpartition("/")[2] == "Cookies" and path != COOKIES:
            raise CapsuleError("the capsule holds a cookie store Team does not check")
        self._seen.add(path)
        if len(self._seen) > MAX_ENTRIES:
            raise CapsuleError("the capsule holds more entries than allowed")
        if directory:
            self._directories.add(path)
            return
        self._total += size
        if self._total > MAX_TOTAL_BYTES:
            raise CapsuleError("the capsule is larger than allowed")
        self._remaining, self._padding = size, (BLOCK - size % BLOCK) % BLOCK
        self._keeping = path == COOKIES

    def finish(self) -> None:
        if not self._done or self._pending:
            raise CapsuleError("the capsule ended early or has bytes after its end")

    def identity_hosts(self) -> frozenset[str]:
        """The identity providers' sign-in hosts some cookie of the profile is sent to; all of them when unreadable."""
        if not self.cookies:
            return IDENTITY_HOSTS
        try:
            hosts = _cookie_hosts(bytes(self.cookies))
        except sqlite3.Error:
            return IDENTITY_HOSTS
        return frozenset(host for host in IDENTITY_HOSTS if any(_sent_to(cookie, host) for cookie in hosts))


def _cookie_hosts(store: bytes) -> set[str]:
    """Every first-party cookie's `host_key`, read only as Chromium's own `cookies` table, within MAX_SQL_STEPS."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
        steps = 0

        def bounded() -> int:
            nonlocal steps
            steps += _PROGRESS_EVERY
            return int(steps > MAX_SQL_STEPS)

        connection.set_progress_handler(bounded, _PROGRESS_EVERY)
        connection.deserialize(store)
        kind = connection.execute("SELECT type FROM sqlite_schema WHERE name = 'cookies'").fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(cookies)")}
        if kind != [("table",)] or not {"host_key", "top_frame_site_key"} <= columns:
            raise sqlite3.DatabaseError("the cookie store is not Chromium's")
        # A partitioned cookie was set under another top-level site and is never sent to the provider's own pages.
        rows = connection.execute("SELECT host_key FROM cookies WHERE top_frame_site_key = ''")
        return {str(row[0]).lower() for row in rows}
    finally:
        connection.close()


def _sent_to(cookie_host: str, host: str) -> bool:
    """Whether a cookie of `cookie_host` is sent to `host`: its own host, or a domain cookie of a parent domain."""
    if cookie_host.startswith("."):
        domain = cookie_host[1:]
        return host == domain or host.endswith(f".{domain}")
    return host == cookie_host
