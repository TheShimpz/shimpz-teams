"""A Team's Sign-in: the sealed browser session a person saved their logins into (ADR-0117, 2026-10-20 amendment).

Each Team has at most one Sign-in. Its capsule is kept as numbered chunk files, each sealed (AES-256-GCM) under a
keyring apart from the state and bound to the Team, its incarnation (the Team network's id), the Sign-in, its
generation, and the chunk's place, so a chunk never moves between capsules. Its record (state, sites found in its
cookies, times, chunk count, writer image, open checkout, deletion fence) is sealed too, bound to the Team and the
Sign-in, so neither the capsule nor its metadata can be altered without the key.

The Sign-in has one exclusive checkout at a time, recorded before its Browser starts. Only a Save commits: it writes the
next generation with compare-and-swap on the checkout and the generation it restored, and leaves the Sign-in `ready`.
Any other end of a checkout that restored a generation keeps that generation's bytes but marks it `needs-person`; the
end of a Team's first checkout leaves no Sign-in. A stale end, of a checkout no longer current, changes nothing. A
Sign-in lives 30 days from its last Save and 7 days unused; an expired one is deleted when read.

A capsule restores only into the exact Browser image that wrote it (ADR-0117 item 6 allows the same Chromium version or
one the reading image proves; no image proves another yet). A person's checkout of a capsule another image wrote starts
with no capsule and needs a new sign-in, and such a Sign-in shows as needing a person.

A Supervisor's deletion records a durable fence first, holding the deletion's activity metadata (ADR-0120), then removes
every capsule and draft and proves them gone; the fenced record stays, as a tombstone no checkout passes, until its
completion is recorded, so a Team that restarts meanwhile still records it. A Team process starting holds no checkout
and no Save, so it ends every open checkout as failed, removes what every fenced deletion left, and deletes every draft
and every capsule no record names.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from local.validation import validate_team_id
from protocol.http.v1 import sign_in as sign_in_protocol
from storage import private_state

STATE_ROOT = Path("/var/lib/shimpz-local/browser-sign-ins/state")
KEY_PATH = Path("/var/lib/shimpz-local/browser-sign-ins/key/aes256.key")
SCHEMA = 2
MAX_STATE_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 8192
CHUNK_BYTES = 1024 * 1024
MAX_CHUNKS = 160
LIFETIME_SECONDS = 30 * 24 * 3600
IDLE_SECONDS = 7 * 24 * 3600
READY, NEEDS_PERSON = "ready", "needs-person"
PERSON, AGENT = "person", "agent"
_RECORD_KEYS = frozenset(
    {
        "team_id",
        "sign_in_id",
        "incarnation",
        "generation",
        "state",
        "sites",
        "sites_truncated",
        "created_at",
        "saved_at",
        "last_used_at",
        "chunks",
        "writer",
        "checkout",
        "deleting",
    }
)


# A deletion's activity metadata (ADR-0120), kept in its fence until its completion is recorded; an empty event id marks
# a deletion of the Team's own lifecycle (expiry, a new incarnation, the Team's deletion), which records its own event
# where it has one (an expiry, through `SignInStore.expired`; the Team's deletion, in its own operation).
DELETING_KEYS = frozenset({"event_id", "initiator_id", "factor", "origin"})
_INTERNAL = dict.fromkeys(DELETING_KEYS, "")


class SignInError(RuntimeError):
    """The Sign-in could not be read or kept as its contract requires; `code` names a closed refusal if one applies."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


_PRIVATE = private_state.PrivateState(
    SignInError,
    "the Sign-ins state is malformed",
    "a Sign-in record is malformed",
    MAX_RECORD_BYTES * 2 + 128,
)


@dataclass(frozen=True, slots=True)
class Checkout:
    """One exclusive checkout of a Team's Sign-in: whose it is, and the generation it restored (0 for a first one)."""

    id: str
    team_id: str
    sign_in_id: str
    incarnation: str
    kind: str
    generation: int


def _record_aad(team_id: str, sign_in_id: str) -> bytes:
    return "\0".join(("shimpz-sign-in-record", team_id, sign_in_id)).encode()


def _chunk_aad(checkout: Checkout, generation: int, index: int) -> bytes:
    parts = ("shimpz-sign-in-capsule", checkout.team_id, checkout.incarnation, checkout.sign_in_id)
    return "\0".join((*parts, str(generation), str(index))).encode()


class SignInStore:
    """Every Team's Sign-in on this Local."""

    def __init__(
        self,
        state_root: Path = STATE_ROOT,
        key_path: Path = KEY_PATH,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        index, self._key_path = _PRIVATE.separate_paths(state_root / "index.json", key_path, "Sign-ins")
        self._root = index.parent
        self._index = index
        self._now = now
        self._lock = threading.RLock()
        # Each Team's in-process fence: a Save writes and commits only if no deletion or reset came since it began.
        self._fences: dict[str, int] = {}
        # Each Team's generation whose publication is unknown: it needs a person until that is durably recorded.
        self._quarantine: dict[str, int] = {}
        # Told of each expired Sign-in deleted as it was read: its Team, generation, and incarnation.
        self.expired: Callable[[str, int, str], None] = lambda _team_id, _generation, _incarnation: None

    # State

    def _key(self, *, create: bool = False) -> bytes:
        return _PRIVATE.key(self._key_path, "Sign-ins keyring", allow_create=create)

    def _state(self) -> dict[str, object]:
        payload = _PRIVATE.read_private_file(self._index, MAX_STATE_BYTES, "Sign-ins state")
        if payload is None:
            return {}
        try:
            value = json.loads(payload)
        except ValueError as exc:
            raise SignInError("the Sign-ins state is malformed") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "teams"} or value["schema"] != SCHEMA:
            raise SignInError("the Sign-ins state is malformed")
        if not isinstance(value["teams"], dict):
            raise SignInError("the Sign-ins state is malformed")
        return value["teams"]

    def _write(self, teams: dict[str, object]) -> None:
        _PRIVATE.write_json(self._index, {"schema": SCHEMA, "teams": teams}, MAX_STATE_BYTES, "Sign-ins state")

    def _record(self, team_id: str) -> tuple[dict[str, object], dict[str, object] | None]:
        """The state, and the Team's record opened and checked, or None when it has none."""
        teams = self._state()
        envelope = teams.get(team_id)
        if envelope is None:
            return teams, None
        if not isinstance(envelope, dict) or set(envelope) != {"sign_in_id", "record"}:
            raise SignInError("a Sign-in record is malformed")
        sign_in_id = envelope["sign_in_id"]
        if not isinstance(sign_in_id, str) or not sign_in_protocol.ID_RE.fullmatch(sign_in_id):
            raise SignInError("a Sign-in record is malformed")
        _PRIVATE.check_identified_envelope(envelope["record"], MAX_RECORD_BYTES, "a Sign-in record is malformed")
        plaintext = _PRIVATE.open_identified_envelope(
            self._key(),
            envelope["record"],
            _record_aad(team_id, sign_in_id),
            MAX_RECORD_BYTES,
            "a Sign-in record is invalid",
        )
        try:
            record = json.loads(plaintext)
        except ValueError as exc:
            raise SignInError("a Sign-in record is invalid") from exc
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_KEYS
            or record["team_id"] != team_id
            or record["sign_in_id"] != sign_in_id
            or not _deleting_admitted(record["deleting"])
        ):
            raise SignInError("a Sign-in record is invalid")
        return teams, record

    def _put(self, teams: dict[str, object], record: dict[str, object]) -> None:
        plaintext = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        team_id, sign_in_id = str(record["team_id"]), str(record["sign_in_id"])
        sealed = private_state.seal_identified(self._key(create=True), plaintext, _record_aad(team_id, sign_in_id))
        teams[team_id] = {"sign_in_id": sign_in_id, "record": sealed}
        self._write(teams)

    def _capsule_dir(self, sign_in_id: str, generation: int) -> Path:
        return self._root / "capsules" / f"{sign_in_id}-{generation}"

    def _drafts(self, team_id: str) -> Path:
        return self._root / "drafts" / team_id

    @staticmethod
    def _remove(directory: Path) -> None:
        """Delete one directory and prove it gone; one already absent is no failure."""
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SignInError("a Sign-in's capsule could not be deleted") from exc
        if directory.exists():
            raise SignInError("a Sign-in's capsule could not be deleted")

    def _expired(self, record: dict[str, object]) -> bool:
        return bool(record["generation"]) and self._now() >= _expires(record)

    def _settle_quarantine(self, team_id: str) -> None:
        """Record a generation whose publication was unknown as needing a person, durably; until then it stays held.

        Published after all, it needs a person's Save before an agent uses it; not published, there is nothing to hold.
        """
        generation = self._quarantine.get(team_id)
        if generation is None:
            return
        teams, record = self._record(team_id)
        if record is not None and record["generation"] == generation and record["state"] != NEEDS_PERSON:
            record["state"] = NEEDS_PERSON
            try:
                self._put(teams, record)
            except SignInError:
                return
        del self._quarantine[team_id]

    def _expire(self, team_id: str, record: dict[str, object]) -> bool:
        """Delete an expired Sign-in, and tell `expired` once it is gone; whether it is."""
        gone = self._delete(team_id)
        if gone:
            self.expired(team_id, int(record["generation"]), str(record["incarnation"]))
        return gone

    # Reading

    def current(self, team_id: str, reader: str | None) -> dict[str, object] | None:
        """The Team's Sign-in as Admin shows it, or None; an expired one is deleted as it is read.

        One the `reader` image cannot restore shows as needing a person.
        """
        team_id = validate_team_id(team_id)
        with self._lock:
            self._settle_quarantine(team_id)
            _teams, record = self._record(team_id)
            if record is None or record["deleting"] or not record["generation"]:
                return None
            if self._expired(record):
                self._expire(team_id, record)
                return None
            public = _public(record)
            if record["writer"] != reader or team_id in self._quarantine:
                public["state"] = NEEDS_PERSON
            return public

    # Checkout

    def checkout(self, team_id: str, incarnation: str, kind: str) -> Checkout:
        """Check the Team's Sign-in out, durably, before its Browser starts; a first one has no generation yet."""
        team_id = validate_team_id(team_id)
        with self._lock:
            self._settle_quarantine(team_id)
            teams, record = self._record(team_id)
            if record is not None and record["deleting"]:
                raise SignInError("the Sign-in is being deleted", sign_in_protocol.BUSY)
            if record is not None and (self._expired(record) or record["incarnation"] != incarnation):
                # An expired or earlier incarnation's Sign-in is gone before anything new is checked out.
                gone = self._expire(team_id, record) if self._expired(record) else self._delete(team_id)
                if not gone:
                    raise SignInError("the Sign-in is being deleted", sign_in_protocol.BUSY)
                teams, record = self._record(team_id)
            if record is not None and record["checkout"] is not None:
                raise SignInError("the Sign-in is checked out", sign_in_protocol.BUSY)
            if (
                record is not None
                and kind == AGENT
                and (record["state"] == NEEDS_PERSON or team_id in self._quarantine)
            ):
                raise SignInError("the Sign-in needs a person", sign_in_protocol.BUSY)
            now = self._now()
            if record is None:
                record = _first(team_id, incarnation, now)
            checkout = Checkout(
                secrets.token_hex(16), team_id, str(record["sign_in_id"]), incarnation, kind, int(record["generation"])
            )
            record["checkout"] = {"id": checkout.id, "kind": kind}
            record["last_used_at"] = max(now, float(record["last_used_at"]))
            self._put(teams, record)
            self._fences.setdefault(team_id, 0)
            return checkout

    def _held(self, checkout: Checkout) -> tuple[dict[str, object], dict[str, object]] | None:
        """The state and record while `checkout` is still the Sign-in's current one, else None."""
        teams, record = self._record(checkout.team_id)
        if (
            record is None
            or record["deleting"]
            or record["checkout"] is None
            or record["checkout"]["id"] != checkout.id
            or record["generation"] != checkout.generation
        ):
            return None
        return teams, record

    def capsule(self, checkout: Checkout, reader: str) -> Iterator[bytes] | None:
        """The checked-out generation's capsule, chunk by chunk, each authenticated, for the `reader` image to restore.

        None for a first checkout, and for a person's checkout of a capsule another image wrote: they sign in anew.
        """
        with self._lock:
            held = self._held(checkout)
            if held is None:
                raise SignInError("the checkout is no longer current", sign_in_protocol.NOT_RUNNING)
            record = held[1]
            if checkout.generation and record["writer"] != reader:
                if checkout.kind != PERSON:
                    raise SignInError("the Sign-in needs a person", sign_in_protocol.BUSY)
                return None
            chunks = int(record["chunks"])
        return self._chunks(checkout, chunks) if checkout.generation else None

    def _chunks(self, checkout: Checkout, chunks: int) -> Iterator[bytes]:
        cipher = AESGCM(self._key())
        directory = self._capsule_dir(checkout.sign_in_id, checkout.generation)
        for index in range(chunks):
            try:
                sealed = (directory / f"{index:05d}.chunk").read_bytes()
                yield cipher.decrypt(sealed[:12], sealed[12:], _chunk_aad(checkout, checkout.generation, index))
            except (OSError, InvalidTag) as exc:
                raise SignInError("the Sign-in's capsule is unreadable or not its own") from exc

    def draft(self, checkout: Checkout) -> Draft:
        """The next generation's capsule, sealed chunk by chunk as it arrives; nothing is kept until it commits."""
        with self._lock:
            if self._held(checkout) is None:
                raise SignInError("the checkout is no longer current", sign_in_protocol.NOT_RUNNING)
            return Draft(self, checkout, self._fences.setdefault(checkout.team_id, 0))

    def finish(self, checkout: Checkout) -> None:
        """End a checkout without a Save: a restored generation now needs a person; a first one leaves nothing."""
        with self._lock:
            held = self._held(checkout)
            if held is None:
                return
            teams, record = held
            if not record["generation"]:
                teams.pop(checkout.team_id, None)
                self._write(teams)
                return
            record["checkout"] = None
            record["state"] = NEEDS_PERSON
            self._put(teams, record)

    # Deleting

    def begin_delete(self, team_id: str, expected: tuple[str, int, str], deleting: dict[str, str]) -> None:
        """Fence the exact Sign-in the Supervisor confirmed (its id, generation, and Team incarnation) for deletion.

        The fence keeps the deletion's activity metadata until its completion is recorded.
        """
        team_id = validate_team_id(team_id)
        if not deleting.get("event_id") or not _deleting_admitted(deleting):
            raise SignInError("the deletion's metadata is not admissible")
        with self._lock:
            teams, record = self._record(team_id)
            if record is None or not record["generation"] or record["deleting"]:
                raise SignInError("the Team has no Sign-in", sign_in_protocol.ABSENT)
            if (record["sign_in_id"], record["generation"], record["incarnation"]) != expected:
                raise SignInError("the Sign-in changed since it was confirmed", sign_in_protocol.CHANGED)
            self._fence(teams, record, dict(deleting))

    def _fence(self, teams: dict[str, object], record: dict[str, object], deleting: dict[str, str]) -> None:
        team_id = str(record["team_id"])
        self._fences[team_id] = self._fences.get(team_id, 0) + 1
        if not record["deleting"]:
            record["deleting"] = deleting
            self._put(teams, record)

    def deletions(self) -> list[tuple[str, dict[str, object]]]:
        """Every Supervisor deletion not recorded as completed yet: its Team, metadata, generation, and incarnation."""
        with self._lock:
            pending = []
            for team_id in self._state():
                _teams, record = self._record(team_id)
                deleting = record["deleting"]
                if deleting and deleting["event_id"]:
                    named = {"generation": record["generation"], "incarnation": record["incarnation"]}
                    pending.append((team_id, {**deleting, **named}))
            return pending

    def purge(self, team_id: str, event_id: str) -> bool:
        """Remove what the deletion `event_id` fenced, its capsules and drafts; whether nothing of it is left.

        Its record stays, fenced, until `settle`. A Sign-in no longer fenced by it was already deleted, and one there
        now was started afterwards: it stays.
        """
        team_id = validate_team_id(team_id)
        with self._lock:
            _teams, record = self._record(team_id)
            if record is None or not record["deleting"] or record["deleting"]["event_id"] != event_id:
                return True
            return self._purge(team_id, record)

    def settle(self, team_id: str, event_id: str) -> bool:
        """Drop the deletion `event_id`'s fenced record once its completion is recorded; whether it is gone."""
        team_id = validate_team_id(team_id)
        with self._lock:
            teams, record = self._record(team_id)
            if record is None or not record["deleting"] or record["deleting"]["event_id"] != event_id:
                return True
            del teams[team_id]
            self._write(teams)
            return team_id not in self._state()

    def _purge(self, team_id: str, record: dict[str, object] | None) -> bool:
        try:
            for directory in self._capsules_of(str(record["sign_in_id"])) if record is not None else ():
                self._remove(directory)
            self._remove(self._drafts(team_id))
        except SignInError:
            return False
        return True

    def _delete(self, team_id: str) -> bool:
        """Delete the Team's Sign-in for its own lifecycle: capsules, drafts, and record; whether nothing is left.

        A Supervisor's deletion under way keeps its fenced record, which holds nothing but its activity metadata, until
        its completion is recorded.
        """
        teams, record = self._record(team_id)
        self._fences[team_id] = self._fences.get(team_id, 0) + 1
        if record is not None:
            self._fence(teams, record, dict(_INTERNAL))
        # From the durable fence on, nothing checks the held generation out; the hold goes only once it is gone.
        if not self._purge(team_id, record):
            return False
        if record is not None and record["deleting"]["event_id"]:
            self._quarantine.pop(team_id, None)
            return True
        teams, _record = self._record(team_id)
        if teams.pop(team_id, None) is not None:
            self._write(teams)
        # The record's removal is durable here (a failed write raised): nothing is left to hold.
        self._quarantine.pop(team_id, None)
        return team_id not in self._state()

    def _capsules_of(self, sign_in_id: str) -> list[Path]:
        capsules = self._root / "capsules"
        if not capsules.is_dir():
            return []
        return [path for path in capsules.iterdir() if path.name.rpartition("-")[0] == sign_in_id]

    def delete_team(self, team_id: str) -> bool:
        """A deleted Team's Sign-in goes with it, fencing a Save under way; whether nothing is left."""
        team_id = validate_team_id(team_id)
        with self._lock:
            return self._delete(team_id)

    def delete_all(self) -> None:
        """A Space reset: every Sign-in and every capsule is deleted, and every Save under way fenced.

        Only the fenced records of Supervisor deletions under way stay, holding nothing but their activity metadata,
        until their completions are recorded.
        """
        with self._lock:
            teams = self._state()
            for team_id in set(self._fences) | set(teams):
                self._fences[team_id] = self._fences.get(team_id, 0) + 1
            pending = {team_id for team_id, _metadata in self.deletions()}
            self._remove(self._root / "capsules")
            self._remove(self._root / "drafts")
            if pending:
                self._write({team_id: teams[team_id] for team_id in pending})
                return
            try:
                self._index.unlink(missing_ok=True)
            except OSError as exc:
                raise SignInError("the Sign-ins were not all deleted") from exc

    def reconcile(self) -> None:
        """At Team startup, end every open checkout as failed and remove what every fenced deletion left.

        A Supervisor's deletion keeps its fenced record until its completion is recorded. Then every draft and every
        capsule no record names is deleted.
        """
        with self._lock:
            for team_id in list(self._state()):
                _teams, record = self._record(team_id)
                if record["deleting"] and record["deleting"]["event_id"]:
                    self._purge(team_id, record)
                elif record["deleting"]:
                    self._delete(team_id)
                elif record["checkout"] is not None:
                    self.finish(
                        Checkout(
                            str(record["checkout"]["id"]),
                            team_id,
                            str(record["sign_in_id"]),
                            str(record["incarnation"]),
                            str(record["checkout"]["kind"]),
                            int(record["generation"]),
                        )
                    )
            named = set()
            for team_id in self._state():
                _teams, record = self._record(team_id)
                named.add(f"{record['sign_in_id']}-{record['generation']}")
            self._remove(self._root / "drafts")
            capsules = self._root / "capsules"
            for directory in capsules.iterdir() if capsules.is_dir() else ():
                if directory.name not in named:
                    self._remove(directory)


class Draft:
    """One Save of a checked-out Sign-in: its chunks sealed as they arrive, its record written only by `commit`."""

    def __init__(self, store: SignInStore, checkout: Checkout, fence: int) -> None:
        self._store = store
        self._checkout = checkout
        self._fence = fence
        self.generation = checkout.generation + 1
        self.chunks = 0
        self._directory = store._drafts(checkout.team_id) / checkout.id
        self._cipher: AESGCM | None = None
        # Set when the commit's outcome is unknown: what it wrote stays for the next Team start to reconcile.
        self._uncertain = False

    def add(self, chunk: bytes) -> None:
        """Seal one more chunk of the capsule, under the store's lock and its Team's fence."""
        if not chunk or len(chunk) > CHUNK_BYTES or self.chunks >= MAX_CHUNKS:
            raise SignInError("the capsule is not within its bounds")
        store = self._store
        with store._lock:
            if store._fences.get(self._checkout.team_id, 0) != self._fence:
                raise SignInError("the Sign-in was revoked before it was kept")
            if self._cipher is None:
                self._cipher = AESGCM(store._key(create=True))
                self._directory.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                self._directory.mkdir(mode=0o700)
            nonce = os.urandom(12)
            aad = _chunk_aad(self._checkout, self.generation, self.chunks)
            sealed = nonce + self._cipher.encrypt(nonce, chunk, aad)
            try:
                private_state.replace_durably(self._directory / f"{self.chunks:05d}.chunk", sealed)
            except OSError as exc:
                raise SignInError("the capsule could not be kept") from exc
        self.chunks += 1

    def commit(self, sites: list[str], truncated: bool, writer: str, admitted: Callable[[], bool]) -> dict[str, object]:
        """Commit the next generation, only if the checkout is still current, nothing fenced it, and `admitted()`."""
        if not self.chunks or not all(sign_in_protocol.canonical_site(site) for site in sites):
            raise SignInError("the Sign-in's capsule or sites are not admissible")
        store, checkout = self._store, self._checkout
        with store._lock:
            held = store._held(checkout)
            if held is None or store._fences.get(checkout.team_id, 0) != self._fence or not admitted():
                raise SignInError("the Sign-in was revoked before it was kept")
            teams, record = held
            private_state.fsync_directory(self._directory)
            capsule = store._capsule_dir(checkout.sign_in_id, self.generation)
            capsule.parent.mkdir(mode=0o700, exist_ok=True)
            try:
                self._directory.rename(capsule)
                # From here a discard deletes the capsule where it now is.
                self._directory = capsule
                private_state.fsync_directory(capsule.parent)
            except OSError as exc:
                raise SignInError("the Sign-in's capsule could not be put in place") from exc
            now = store._now()
            earlier = int(record["generation"])
            record.update(
                generation=self.generation,
                state=READY,
                sites=sorted(sites)[: sign_in_protocol.MAX_SITES],
                sites_truncated=truncated,
                saved_at=now,
                last_used_at=now,
                chunks=self.chunks,
                writer=writer,
                checkout=None,
            )
            try:
                store._put(teams, record)
            except SignInError:
                if not self._published(record):
                    raise
            if checkout.kind == PERSON:
                # A person's Save resolves whatever an earlier unknown publication left.
                store._quarantine.pop(checkout.team_id, None)
        if earlier:
            # The generation this one replaced; one left behind is deleted at the next Team start.
            shutil.rmtree(store._capsule_dir(checkout.sign_in_id, earlier), ignore_errors=True)
        return _public(record)

    def _published(self, record: dict[str, object]) -> bool:
        """After a failed write of the index: whether it names this generation anyway (its replacement happened).

        Unknowable, the capsule stays where it is and the Save is refused; the next Team start keeps or deletes it.
        """
        try:
            _teams, kept = self._store._record(self._checkout.team_id)
            if kept != record:
                return False
            # Named, it is kept only once that naming is durable too.
            private_state.fsync_directory(self._store._index.parent)
        except (SignInError, OSError) as exc:
            self._uncertain = True
            self._store._quarantine[self._checkout.team_id] = self.generation
            raise SignInError("the Sign-in's publication is unknown") from exc
        return True

    def discard(self) -> None:
        """Delete what the Save wrote; anything left is deleted when the Team process next starts."""
        if not self._uncertain:
            shutil.rmtree(self._directory, ignore_errors=True)


def _first(team_id: str, incarnation: str, now: float) -> dict[str, object]:
    """A Team's first Sign-in, before any Save: generation 0, shown to no one."""
    return {
        "team_id": team_id,
        "sign_in_id": secrets.token_hex(16),
        "incarnation": incarnation,
        "generation": 0,
        "state": READY,
        "sites": [],
        "sites_truncated": False,
        "created_at": now,
        "saved_at": now,
        "last_used_at": now,
        "chunks": 0,
        "writer": "",
        "checkout": None,
        "deleting": None,
    }


def _deleting_admitted(value: object) -> bool:
    """A fence's metadata: none, or exactly its keys, each a bounded string (empty for a lifecycle deletion)."""
    return value is None or (
        isinstance(value, dict)
        and set(value) == DELETING_KEYS
        and all(isinstance(item, str) and len(item) <= 255 for item in value.values())
    )


def _expires(record: dict[str, object]) -> float:
    return min(float(record["saved_at"]) + LIFETIME_SECONDS, float(record["last_used_at"]) + IDLE_SECONDS)


def _public(record: dict[str, object]) -> dict[str, object]:
    """What a Supervisor sees of the Sign-in: never its capsule."""
    return {
        "sign_in_id": record["sign_in_id"],
        "state": record["state"],
        "generation": record["generation"],
        "team_incarnation": record["incarnation"],
        "sites": list(record["sites"]),
        "sites_truncated": record["sites_truncated"],
        "created_at": _iso(float(record["created_at"])),
        "saved_at": _iso(float(record["saved_at"])),
        "last_used_at": _iso(float(record["last_used_at"])),
        "expires_at": _iso(_expires(record)),
    }


def _iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
