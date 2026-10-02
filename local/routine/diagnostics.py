"""Encrypted per-execution diagnostics of a Local Team's Routine runs (ADR-0092 section 8).

Each attempt's Team-sanitized failure diagnostic, or its safe transport condition, is one AES-256-GCM file in its own
Team-owned blob family, apart from plaintext Routine state, continuations, and their keyring. The AAD binds the Team
and its incarnation (its network id), the Routine, run, logical operation, attempt, and recording instant, so a body is
readable only by the same incarnation of the same Team as exactly that attempt. Each body names its incarnation under
that authentication, so another incarnation's authentic body is left out while any corrupted body fails the read. A
body is at most 16 KiB, expires after seven days, and a Team keeps at most 10 MiB, the oldest giving way first: bodies
are diagnostics, never the compact safety evidence an incident keeps. A body never holds a password or any other value
Team injected.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import strict_json
from local.errors import ApiProblemError as ApiProblem
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from storage import private_state

ROOT = Path("/var/lib/shimpz-local/routines/diagnostics")
KEY_PATH = Path("/var/lib/shimpz-local/routines/diagnostics-key/aes256.key")
RETENTION_SECONDS = 7 * 86_400
MAX_FILE_BYTES = 16 * 1024
MAX_PLAINTEXT_BYTES = 12 * 1024
MAX_TEAM_BYTES = 10 * 1024 * 1024
_TEAM_DIR_RE = re.compile(r"[0-9a-f]{64}\Z")
_INCARNATION_RE = re.compile(r"[0-9a-f]{64}\Z")
_NAME_RE = re.compile(
    r"(?P<at>[0-9]{1,12})\.(?P<routine>[0-9a-f]{32})\.(?P<run>[0-9a-f]{32})\."
    r"(?P<operation>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.(?P<attempt>[0-9]{1,2})"
    r"\.diagnostic\Z"
)


class DiagnosticStoreError(RuntimeError):
    """Diagnostic state is invalid or unavailable; callers fail closed."""


_PRIVATE = private_state.PrivateState(
    DiagnosticStoreError,
    "Routine diagnostic state is malformed",
    "Routine diagnostic is malformed",
    MAX_FILE_BYTES,
)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One attempt's diagnostic, keyed by the Routine run and logical operation it belongs to."""

    routine_id: str
    run_id: str
    operation_id: str
    attempt: int
    assistant_id: str
    action: str
    recorded_at: int
    failure: dict[str, object] | None = None
    condition: str | None = None

    def view(self) -> dict[str, object]:
        """The wire form a Supervisor reads, which ``routine.canonical_diagnostic`` admits."""
        instant = datetime.datetime.fromtimestamp(self.recorded_at, datetime.UTC)
        return {
            "operation_id": self.operation_id,
            "attempt": self.attempt,
            "assistant_id": self.assistant_id,
            "action": self.action,
            "recorded_at": instant.isoformat().replace("+00:00", "Z"),
            "failure": self.failure,
            "condition": self.condition,
        }

    def name(self) -> str:
        return f"{self.recorded_at}.{self.routine_id}.{self.run_id}.{self.operation_id}.{self.attempt}.diagnostic"


def _aad(team_id: str, incarnation: str, name: str) -> bytes:
    return json.dumps(
        ["shimpz-local-routine-diagnostic-v1", team_id, incarnation, name], separators=(",", ":")
    ).encode()


def _incarnation(value: object) -> str:
    if not isinstance(value, str) or _INCARNATION_RE.fullmatch(value) is None:
        raise DiagnosticStoreError("Routine diagnostic incarnation is invalid")
    return value


class DiagnosticStore:
    """Every Local Team's encrypted Routine diagnostics, in one blob family with its own keyring."""

    def __init__(self, root: Path = ROOT, key_path: Path = KEY_PATH) -> None:
        self.root = Path(root)
        self.key_path = Path(key_path)
        if self.key_path.parent == self.root or self.root in self.key_path.parents:
            raise DiagnosticStoreError("Routine diagnostic keyring must be separate from its bodies")
        self._guard = threading.Lock()

    def _team_dir(self, team_id: str) -> Path:
        return self.root / hashlib.sha256(team_id.encode()).hexdigest()

    def record(self, team_id: str, incarnation: str, diagnostic: Diagnostic, protected: Iterable[str]) -> None:
        """Seal one attempt's diagnostic, first removing expired bodies and then the oldest beyond the Team bound."""
        team, incarnation = validate_team_id(team_id), _incarnation(incarnation)
        view = diagnostic.view()
        if (
            http_routine.canonical_diagnostic(view) is None
            or http_routine.ROUTINE_ID_RE.fullmatch(diagnostic.routine_id) is None
            or http_routine.ROUTINE_ID_RE.fullmatch(diagnostic.run_id) is None
        ):
            raise DiagnosticStoreError("Routine diagnostic is invalid")
        payload = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > MAX_PLAINTEXT_BYTES:
            raise DiagnosticStoreError("Routine diagnostic exceeds its fixed byte limit")
        if any(secret and secret.encode() in payload for secret in protected):
            raise DiagnosticStoreError("Routine diagnostic would hold a protected value")
        name = diagnostic.name()
        with self._guard:
            key = _PRIVATE.key(self.key_path, "Routine diagnostic keyring", allow_create=True)
            nonce = os.urandom(12)
            envelope = json.dumps(
                {
                    "algorithm": "AES-256-GCM",
                    # The authenticated origin: the AAD binds it, so a reader can tell another incarnation's body
                    # from a corrupted one.
                    "incarnation": incarnation,
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "ciphertext": base64.b64encode(
                        AESGCM(key).encrypt(nonce, payload, _aad(team, incarnation, name))
                    ).decode("ascii"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            directory = self._team_dir(team)
            self._make_room(directory, diagnostic, len(envelope))
            _PRIVATE.atomic_write(directory / name, envelope, "Routine diagnostic")

    def _make_room(self, directory: Path, diagnostic: Diagnostic, size: int) -> None:
        """Remove expired bodies and this attempt's earlier body, then the oldest while the new one does not fit."""
        entries = self._entries(directory)
        kept = []
        for name, match, existing in entries:
            same = (match["routine"], match["run"], match["operation"], int(match["attempt"])) == (
                diagnostic.routine_id,
                diagnostic.run_id,
                diagnostic.operation_id,
                diagnostic.attempt,
            )
            if same or int(match["at"]) <= diagnostic.recorded_at - RETENTION_SECONDS:
                self._unlink(directory / name)
            else:
                kept.append((name, existing))
        total = sum(existing for _name, existing in kept)
        for name, existing in kept:
            if total + size <= MAX_TEAM_BYTES:
                break
            self._unlink(directory / name)
            total -= existing

    def read(self, team_id: str, incarnation: str, run_id: str, now: int) -> tuple[Diagnostic, ...]:
        """One run's unexpired diagnostics that open under this exact Team incarnation, oldest first."""
        team, incarnation = validate_team_id(team_id), _incarnation(incarnation)
        if http_routine.ROUTINE_ID_RE.fullmatch(run_id) is None:
            raise DiagnosticStoreError("Routine run is invalid")
        directory = self._team_dir(team)
        found = []
        with self._guard:
            for name, match, _size in self._entries(directory):
                if match["run"] != run_id or int(match["at"]) <= now - RETENTION_SECONDS:
                    continue
                opened = self._open(directory / name, team, incarnation, name)
                if opened is not None:
                    found.append(_diagnostic(match, opened))
        found.sort(key=lambda item: (item.recorded_at, item.operation_id, item.attempt))
        return tuple(found[-http_routine.MAX_RUN_DIAGNOSTICS :])

    def _open(self, path: Path, team: str, incarnation: str, name: str) -> dict[str, object] | None:
        """Decrypt one body under the incarnation it names; another incarnation's authentic body is never shown.

        The body names its incarnation and the AAD binds that name with the Team, the file name, and the content, so
        only an authentic body of another incarnation is left out: any corruption, including a changed incarnation,
        Team, or name, fails authentication and closes the read.
        """
        raw = _PRIVATE.read_private_file(path, MAX_FILE_BYTES, "Routine diagnostic")
        if raw is None:
            return None
        try:
            envelope = strict_json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise DiagnosticStoreError("Routine diagnostic is malformed") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"algorithm", "incarnation", "nonce", "ciphertext"}
            or envelope["algorithm"] != "AES-256-GCM"
            or not isinstance(envelope["incarnation"], str)
            or _INCARNATION_RE.fullmatch(envelope["incarnation"]) is None
        ):
            raise DiagnosticStoreError("Routine diagnostic is malformed")
        try:
            payload = AESGCM(_PRIVATE.key(self.key_path, "Routine diagnostic keyring")).decrypt(
                _PRIVATE.decode_part(envelope["nonce"], expected=12),
                _PRIVATE.decode_part(envelope["ciphertext"], minimum=17, maximum=MAX_PLAINTEXT_BYTES + 16),
                _aad(team, envelope["incarnation"], name),
            )
        except InvalidTag as exc:
            raise DiagnosticStoreError("Routine diagnostic authentication failed") from exc
        if envelope["incarnation"] != incarnation:
            return None
        try:
            view = http_routine.canonical_diagnostic(strict_json.loads(payload))
        except UnicodeDecodeError, ValueError:
            view = None
        if view is None:
            raise DiagnosticStoreError("Routine diagnostic is malformed")
        return view

    def _entries(self, directory: Path) -> list[tuple[str, re.Match[str], int]]:
        """The Team's diagnostic files, oldest first; a non-file where one belongs fails closed."""
        try:
            with os.scandir(directory) as scanned:
                entries = []
                for entry in scanned:
                    match = _NAME_RE.fullmatch(entry.name)
                    if match is None:
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise DiagnosticStoreError("Routine diagnostics failed their ownership contract")
                    entries.append((entry.name, match, metadata.st_size))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise DiagnosticStoreError("Routine diagnostics could not be listed") from exc
        return sorted(entries, key=lambda item: (int(item[1]["at"]), item[0]))

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise DiagnosticStoreError("Routine diagnostic could not be removed") from exc

    def delete_routine(self, team_id: str, routine_id: str) -> None:
        """Remove a deleted Routine's diagnostic bodies; its incidents keep the safety records."""
        directory = self._team_dir(validate_team_id(team_id))
        with self._guard:
            for name, match, _size in self._entries(directory):
                if match["routine"] == routine_id:
                    self._unlink(directory / name)

    def delete(self, team_id: str) -> None:
        """Remove every diagnostic of a deleted Team; an absent Team is already clean."""
        with self._guard:
            self._remove(self._team_dir(validate_team_id(team_id)))

    def delete_all(self) -> None:
        """Remove every Team's diagnostics and the diagnostic keyring, as a Space reset does."""
        with self._guard:
            try:
                names = sorted(entry.name for entry in os.scandir(self.root) if _TEAM_DIR_RE.fullmatch(entry.name))
            except FileNotFoundError:
                names = []
            except OSError as exc:
                raise DiagnosticStoreError("Routine diagnostics could not be listed") from exc
            for name in names:
                self._remove(self.root / name)
            self._unlink(self.key_path)

    def _remove(self, directory: Path) -> None:
        for name, _match, _size in self._entries(directory):
            self._unlink(directory / name)
        try:
            directory.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DiagnosticStoreError("Routine diagnostics could not be removed") from exc


def _diagnostic(match: re.Match[str], view: dict[str, object]) -> Diagnostic:
    return Diagnostic(
        routine_id=match["routine"],
        run_id=match["run"],
        operation_id=view["operation_id"],
        attempt=view["attempt"],
        assistant_id=view["assistant_id"],
        action=view["action"],
        recorded_at=int(match["at"]),
        failure=view["failure"],
        condition=view["condition"],
    )


def run_diagnostics(self, team_id: str, run_id: str, now: int) -> dict[str, object]:
    """A Supervisor's view of one run's execution details in the Team's current incarnation."""
    team_id = validate_team_id(team_id)
    incarnation = self.assistant_lifecycle._network(team_id).id
    try:
        found = self.routine_diagnostics.read(team_id, incarnation, run_id, now)
    except DiagnosticStoreError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Routine diagnostics are unavailable", code="routine-state-unavailable"
        ) from exc
    view = http_routine.canonical_diagnostics(
        {"team_id": team_id, "run_id": run_id, "diagnostics": [item.view() for item in found]}
    )
    if view is None:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Routine diagnostics are unavailable", code="routine-state-unavailable"
        )
    return view
