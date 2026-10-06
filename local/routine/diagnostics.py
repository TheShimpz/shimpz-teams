"""Encrypted per-execution diagnostics of a Local Team's Routine runs (ADR-0092 section 8).

Each attempt's Team-sanitized failure diagnostic, or its safe transport condition, is one AES-256-GCM file in its own
Team-owned blob family, apart from plaintext Routine state, continuations, and their keyring. The family lives in its
own ``diagnostics`` directory of the Routine state volume and its keyring is ``diagnostics.key`` beside the Routine
keyring: neither name is a Routine Team directory or the Routine keyring, so each store ignores the other. The AAD
binds the Team and its incarnation (its network id), the Routine, run, logical operation, attempt, and recording
instant, so a body is readable only by the same incarnation of the same Team as exactly that attempt. Each body names
its incarnation under that authentication, so another incarnation's authentic body is left out while any corrupted
body fails the read. Beside them the family keeps each run's step records, what each step's attempt did with the
redacted inputs it was given, and the run's terminal record, which proves how far the run went (ADR-0092 amendment,
2026-10-05, scale). Every body expires after seven days, and a Team keeps at most 24 MiB of them, the oldest giving way
first whatever its kind: bodies are display records, never the compact safety evidence an incident keeps. A body never
holds a password or any other value Team injected.
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from action import execution as action_execution
from action import failure as action_failure
from local.errors import ApiProblemError as ApiProblem
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import strict_json
from storage import private_state

ROOT = Path("/var/lib/shimpz-local/routines/state/diagnostics")
KEY_PATH = Path("/var/lib/shimpz-local/routines/key/diagnostics.key")
RETENTION_SECONDS = 7 * 86_400
MAX_PLAINTEXT_BYTES = 12 * 1024
# A step record: one run step's wire view and the run binding beside it; a run's terminal record is small.
MAX_STEP_PLAINTEXT_BYTES = http_routine.MAX_STEP_VIEW_BYTES + 2 * 1024
# A run's terminal record holds its decision record, whose quoted rules and rationale may be escaped six times over.
MAX_RUN_PLAINTEXT_BYTES = 20 * 1024
MAX_TEAM_BYTES = 24 * 1024 * 1024


def sealed_bound(plaintext: int) -> int:
    """A sealed file's bytes for this much plaintext: its AES-GCM tag, base64, and JSON envelope."""
    return 4 * -(-(plaintext + 16) // 3) + 256


MAX_FILE_BYTES = sealed_bound(MAX_STEP_PLAINTEXT_BYTES)
_TEAM_DIR_RE = re.compile(r"[0-9a-f]{64}\Z")
_INCARNATION_RE = re.compile(r"[0-9a-f]{64}\Z")
_NAME_RE = re.compile(
    r"(?P<at>[0-9]{1,12})\.(?P<routine>[0-9a-f]{32})\.(?P<run>[0-9a-f]{32})\."
    r"(?P<operation>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.(?P<attempt>[0-9]{1,2})"
    r"\.diagnostic\Z"
)
_STEP_NAME_RE = re.compile(
    r"(?P<at>[0-9]{1,12})\.(?P<routine>[0-9a-f]{32})\.(?P<run>[0-9a-f]{32})\.(?P<sequence>[0-9]{1,5})\.step\Z"
)
_RUN_NAME_RE = re.compile(r"(?P<at>[0-9]{1,12})\.(?P<routine>[0-9a-f]{32})\.(?P<run>[0-9a-f]{32})\.run\Z")
# Each kind of body by its name.
_KINDS = {"diagnostic": _NAME_RE, "step": _STEP_NAME_RE, "run": _RUN_NAME_RE}


def _bound_of(kind: str) -> int:
    """A kind's plaintext bound."""
    return {"diagnostic": MAX_PLAINTEXT_BYTES, "step": MAX_STEP_PLAINTEXT_BYTES, "run": MAX_RUN_PLAINTEXT_BYTES}[kind]


def _forms(secret: str) -> set[bytes]:
    """Every form a protected value can take in a body: raw, JSON-escaped, or preview-escaped, each escaped again."""
    once = json.dumps(secret, ensure_ascii=False)[1:-1]
    shown = http_routine.escaped(once)
    return {
        text.encode()
        for value in (secret, once, shown)
        for text in (value, json.dumps(value, ensure_ascii=False)[1:-1])
    }


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
    # The attempt's call: a replay step by its position in the plan, or a decision call (ADR-0101).
    position: dict[str, object]
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
            "position": self.position,
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


def _instant_text(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RunBinding:
    """The run a record belongs to, the exact revision of its Routine it executed, and that revision's replay steps."""

    routine_id: str
    run_id: str
    revision: int
    plan_digest: str
    total: int

    def document(self) -> dict[str, object]:
        return {
            "routine_id": self.routine_id,
            "revision": self.revision,
            "plan_digest": self.plan_digest,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class StepRecord:
    """What one step's attempt did: its status, how long it took, and the redacted inputs it was given."""

    binding: RunBinding
    position: dict[str, object]
    assistant_id: str
    action: str
    status: str
    attempt: int
    duration_ms: int | None
    recorded_at: int
    inputs: list[dict[str, object]] | None

    def view(self) -> dict[str, object]:
        """The wire form of this step, which ``routine.canonical_run_step`` admits."""
        return {
            "position": self.position,
            "status": self.status,
            "assistant_id": self.assistant_id,
            "action": self.action,
            "attempt": self.attempt,
            "duration_ms": self.duration_ms,
            "recorded_at": _instant_text(self.recorded_at),
            "inputs": self.inputs,
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A run's terminal record: how far its replay went, its decision calls, and its decision record.

    It holds how many steps its sealed cursor completed, whether the next one was dispatched, how many decision calls
    it started, and how its decision ended (ADR-0101 section 7).
    """

    binding: RunBinding
    reached: int
    dispatched: bool
    recorded_at: int
    calls: int = 0
    decision: dict[str, object] | None = None


class RunChangedError(DiagnosticStoreError):
    """A page named a snapshot of a run's records that no longer holds."""


class DiagnosticStore:
    """Every Local Team's encrypted Routine diagnostics and run records, in one blob family with its own keyring."""

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
        attempt = (diagnostic.routine_id, diagnostic.run_id, diagnostic.operation_id, str(diagnostic.attempt))

        def replaced(kind: str, match: re.Match[str]) -> bool:
            return kind == "diagnostic" and (match["routine"], match["run"], match["operation"], match["attempt"]) == (
                attempt
            )

        with self._guard:
            self._seal(
                team, incarnation, (diagnostic.name(), "diagnostic", diagnostic.recorded_at), view, protected, replaced
            )

    def record_step(self, team_id: str, incarnation: str, step: StepRecord, protected: Iterable[str]) -> None:
        """Seal one step record under the run's next sequence; a later one of the same position supersedes it."""
        team, incarnation = validate_team_id(team_id), _incarnation(incarnation)
        document = {**step.binding.document(), **step.view()}
        if not _step_document(document, step.binding.run_id):
            raise DiagnosticStoreError("Routine step record is invalid")
        binding = step.binding
        with self._guard:
            entries = [item for item in self._entries(self._team_dir(team)) if item[2]["run"] == binding.run_id]
            sequence = 1 + max((int(match["sequence"]) for _n, kind, match, _s in entries if kind == "step"), default=0)
            name = f"{step.recorded_at}.{binding.routine_id}.{binding.run_id}.{sequence}.step"
            self._seal(team, incarnation, (name, "step", step.recorded_at), document, protected, lambda *_args: False)

    def record_run(self, team_id: str, incarnation: str, run: RunRecord) -> None:
        """Seal a run's terminal record, which proves which of its steps never started."""
        team, incarnation = validate_team_id(team_id), _incarnation(incarnation)
        binding = run.binding
        document = {
            **binding.document(),
            "reached": run.reached,
            "dispatched": run.dispatched,
            "calls": run.calls,
            "decision": run.decision,
        }
        if not _run_document(document, binding):
            raise DiagnosticStoreError("Routine run record is invalid")
        name = f"{run.recorded_at}.{binding.routine_id}.{binding.run_id}.run"

        def replaced(kind: str, match: re.Match[str]) -> bool:
            return kind == "run" and match["run"] == binding.run_id

        with self._guard:
            self._seal(team, incarnation, (name, "run", run.recorded_at), document, (), replaced)

    def _seal(self, team: str, incarnation: str, named, document, protected: Iterable[str], replaced) -> None:
        """Encrypt one body of its kind, then make room for it and write it; the caller holds the guard."""
        name, kind, recorded_at = named
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > _bound_of(kind):
            raise DiagnosticStoreError("Routine diagnostic exceeds its fixed byte limit")
        if any(form in payload for secret in protected if secret for form in _forms(secret)):
            raise DiagnosticStoreError("Routine diagnostic would hold a protected value")
        key = _PRIVATE.key(self.key_path, "Routine diagnostic keyring", allow_create=True)
        nonce = os.urandom(12)
        envelope = json.dumps(
            {
                "algorithm": "AES-256-GCM",
                # The authenticated origin: the AAD binds it, so a reader can tell another incarnation's body from a
                # corrupted one.
                "incarnation": incarnation,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(
                    AESGCM(key).encrypt(nonce, payload, _aad(team, incarnation, name))
                ).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        # The family's own directory is as private as each Team's, not left to the process umask.
        _PRIVATE.require_private_directory(self.root, "Routine diagnostic")
        directory = self._team_dir(team)
        self._make_room(directory, recorded_at, len(envelope), replaced)
        _PRIVATE.atomic_write(directory / name, envelope, "Routine diagnostic")

    def _make_room(self, directory: Path, recorded_at: int, size: int, replaced) -> None:
        """Remove expired and replaced bodies, then the oldest of any kind while the new one does not fit."""
        kept = []
        for name, kind, match, existing in self._entries(directory):
            if replaced(kind, match) or int(match["at"]) <= recorded_at - RETENTION_SECONDS:
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
            for name, kind, match, _size in self._entries(directory):
                if kind != "diagnostic" or match["run"] != run_id or int(match["at"]) <= now - RETENTION_SECONDS:
                    continue
                opened = self._open(directory / name, team, incarnation, name, MAX_PLAINTEXT_BYTES)
                view = None if opened is None else http_routine.canonical_diagnostic(opened)
                if opened is not None and view is None:
                    raise DiagnosticStoreError("Routine diagnostic is malformed")
                if view is not None:
                    found.append(_diagnostic(match, view))
        found.sort(key=lambda item: (item.recorded_at, item.operation_id, item.attempt))
        return tuple(found[-http_routine.MAX_RUN_DIAGNOSTICS :])

    def run_steps(self, team_id: str, incarnation: str, run_id: str, now: int, page: tuple[str, int]):
        """One run's retained records as one snapshot, assembled under the guard: ``(snapshot, records, run)``.

        ``page`` names the snapshot a reader holds (``latest`` for a fresh one) and its offset; a snapshot whose
        retained bodies changed since, by expiry, eviction, or a new record, raises RunChangedError.
        """
        team, incarnation = validate_team_id(team_id), _incarnation(incarnation)
        directory = self._team_dir(team)
        with self._guard:
            retained = [
                item
                for item in self._entries(directory)
                if item[1] in ("step", "run")
                and item[2]["run"] == run_id
                and int(item[2]["at"]) > now - RETENTION_SECONDS
            ]
            snapshot = _snapshot(directory, retained)
            if page[0] not in ("latest", snapshot):
                raise RunChangedError("Routine run records changed")
            steps: dict[tuple[str, int], tuple[int, dict[str, object]]] = {}
            run = None
            for name, kind, match, _size in retained:
                opened = self._open(directory / name, team, incarnation, name, _bound_of(kind))
                if opened is None:
                    continue
                if kind == "run":
                    run = opened
                    continue
                sequence = int(match["sequence"])
                key = position_key(opened.get("position"))
                if key not in steps or steps[key][0] < sequence:
                    steps[key] = (sequence, opened)
        return snapshot, {key: item for key, (_sequence, item) in steps.items()}, run

    def _open(self, path: Path, team: str, incarnation: str, name: str, maximum: int) -> dict[str, object] | None:
        """Decrypt one body under the incarnation it names; another incarnation's authentic body is never shown.

        The body names its incarnation and the AAD binds that name with the Team, the file name, and the content, so
        only an authentic body of another incarnation is left out: any corruption, including a changed incarnation,
        Team, or name, fails authentication and closes the read.
        """
        raw = _PRIVATE.read_private_file(path, sealed_bound(maximum), "Routine diagnostic")
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
                _PRIVATE.decode_part(envelope["ciphertext"], minimum=17, maximum=maximum + 16),
                _aad(team, envelope["incarnation"], name),
            )
        except InvalidTag as exc:
            raise DiagnosticStoreError("Routine diagnostic authentication failed") from exc
        if envelope["incarnation"] != incarnation:
            return None
        try:
            value = strict_json.loads(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise DiagnosticStoreError("Routine diagnostic is malformed") from exc
        if not isinstance(value, dict):
            raise DiagnosticStoreError("Routine diagnostic is malformed")
        return value

    def _entries(self, directory: Path) -> list[tuple[str, str, re.Match[str], int]]:
        """The Team's bodies of every kind, oldest first; a non-file where one belongs fails closed."""
        try:
            with os.scandir(directory) as scanned:
                entries = []
                for entry in scanned:
                    found = next(
                        ((kind, match) for kind, pattern in _KINDS.items() if (match := pattern.fullmatch(entry.name))),
                        None,
                    )
                    if found is None:
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise DiagnosticStoreError("Routine diagnostics failed their ownership contract")
                    entries.append((entry.name, found[0], found[1], metadata.st_size))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise DiagnosticStoreError("Routine diagnostics could not be listed") from exc
        return sorted(entries, key=lambda item: (int(item[2]["at"]), item[0]))

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise DiagnosticStoreError("Routine diagnostic could not be removed") from exc

    def delete_routine(self, team_id: str, routine_id: str) -> None:
        """Remove a deleted Routine's bodies of every kind; its incidents keep the safety records."""
        directory = self._team_dir(validate_team_id(team_id))
        with self._guard:
            for name, _kind, match, _size in self._entries(directory):
                if match["routine"] == routine_id:
                    self._unlink(directory / name)

    def delete(self, team_id: str) -> None:
        """Remove every body of a deleted Team; an absent Team is already clean."""
        with self._guard:
            self._remove(self._team_dir(validate_team_id(team_id)))

    def delete_all(self) -> None:
        """Remove every Team's bodies and the diagnostic keyring, as a Space reset does."""
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
        for name, _kind, _match, _size in self._entries(directory):
            self._unlink(directory / name)
        try:
            directory.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DiagnosticStoreError("Routine diagnostics could not be removed") from exc


def _snapshot(directory: Path, retained: list[tuple[str, str, re.Match[str], int]]) -> str:
    """The identity of exactly these sealed bodies: their names and the digests of their sealed bytes.

    Every write seals under a fresh nonce, so a body rewritten or recreated under a reused name never reproduces it.
    """
    digest = hashlib.sha256()
    for name, _kind, _match, _size in retained:
        raw = _PRIVATE.read_private_file(directory / name, MAX_FILE_BYTES, "Routine diagnostic") or b""
        digest.update(json.dumps([name, hashlib.sha256(raw).hexdigest()]).encode() + b"\n")
    return digest.hexdigest()[:32]


def position_key(position: object) -> tuple[str, int]:
    """A record's position as a sortable key: replay steps before decision calls, each in order."""
    if not isinstance(position, dict):
        return ("", 0)
    return (str(position.get("phase")), position.get("step", position.get("call", 0)))


def _bound(document: dict[str, object]) -> bool:
    return (
        http_routine.ROUTINE_ID_RE.fullmatch(str(document.get("routine_id"))) is not None
        and type(document.get("revision")) is int
        and 1 <= document["revision"] < 2**31
        and http_routine.PLAN_DIGEST_RE.fullmatch(str(document.get("plan_digest"))) is not None
        and type(document.get("total")) is int
        and 0 <= document["total"] <= http_routine.MAX_ROUTINE_STEPS
    )


def _step_document(document: dict[str, object], run_id: str) -> bool:
    """One sealed step record: its run binding and exactly one run entry's wire view at its own position."""
    view = {key: document.get(key) for key in document if key not in ("routine_id", "revision", "plan_digest", "total")}
    return (
        http_routine.ROUTINE_ID_RE.fullmatch(run_id) is not None
        and _bound(document)
        and view.get("status") in http_routine.RUN_STEP_STATUSES
        and http_routine.canonical_position(view.get("position"), document["total"]) is not None
        and http_routine.canonical_run_step(view, view["position"], document["total"]) is not None
    )


def _run_document(document: dict[str, object], binding: RunBinding) -> bool:
    fields = {"routine_id", "revision", "plan_digest", "total", "reached", "dispatched", "calls", "decision"}
    return (
        http_routine.ROUTINE_ID_RE.fullmatch(binding.run_id) is not None
        and _bound(document)
        and set(document) == fields
        and type(document["reached"]) is int
        and 0 <= document["reached"] <= document["total"]
        and type(document["dispatched"]) is bool
        and type(document["calls"]) is int
        and 0 <= document["calls"] <= http_routine.MAX_DECISION_CALLS
        and (document["decision"] is None or http_routine.canonical_decision_record(document["decision"]) is not None)
    )


def evidence(exc: BaseException, protection: object) -> tuple[dict[str, object] | None, str | None] | None:
    """The sanitized failure or safe transport condition a failed attempt's problem was raised from, or None.

    Only Team's own admitted failure document or a closed transport condition is ever kept, never a message. A failure
    is re-redacted against the run's whole ``protection`` before anything keeps it, and after the run lost its
    protection only its status is kept (ADR-0101 section 6.2).
    """
    failure = action_failure.failure_of(exc)
    if failure is not None:
        if protection.lost:
            return action_failure.withheld(failure).document(), None
        return action_failure.redacted_with(failure, protection.values).document(), None
    cause = exc
    for _depth in range(8):
        if cause is None:
            return None
        if isinstance(cause, action_execution.RpcExchangeError):
            condition = cause.condition
            return (None, condition) if http_routine.CONDITION_RE.fullmatch(condition) else None
        cause = cause.__cause__
    return None


def protected(evidence_value: action_execution.ActionInvocationEvidence) -> tuple[str, ...]:
    """Every value Team injected into one attempt, which its diagnostic must never hold."""
    found: list[str] = list(evidence_value.transcript.protected_values().values())
    found.extend(evidence_value.private_inputs.stored_inputs.values())
    pending: list[object] = [evidence_value.private_inputs.integrations]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, list | tuple):
            pending.extend(value)
    return tuple(found)


def _diagnostic(match: re.Match[str], view: dict[str, object]) -> Diagnostic:
    return Diagnostic(
        routine_id=match["routine"],
        run_id=match["run"],
        operation_id=view["operation_id"],
        attempt=view["attempt"],
        assistant_id=view["assistant_id"],
        action=view["action"],
        position=view["position"],
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


def _gap(position: dict[str, object], status: str) -> dict[str, object]:
    return {
        "position": position,
        "status": status,
        "assistant_id": None,
        "action": None,
        "attempt": None,
        "duration_ms": None,
        "recorded_at": None,
        "inputs": None,
    }


_BINDING_FIELDS = ("routine_id", "revision", "plan_digest", "total")


def _binding_of(records: dict, run: dict[str, object] | None) -> dict[str, object] | None:
    """The run binding every retained record agrees on, or None when none is retained; a disagreement fails closed."""
    bindings = {
        json.dumps({key: item[key] for key in _BINDING_FIELDS}, sort_keys=True)
        for item in [*records.values(), *([run] if run is not None else [])]
    }
    if len(bindings) > 1:
        raise DiagnosticStoreError("Routine run records disagree")
    return json.loads(bindings.pop()) if bindings else None


def _calls(records: dict, run: dict[str, object] | None) -> int:
    """The decision calls a run made: its terminal record's count, or while it runs the highest call recorded."""
    if run is not None:
        return run["calls"]
    return max((number for phase, number in records if phase == "decision"), default=0)


def _entry(records: dict, reached: tuple[int, bool] | None, position: dict[str, object]) -> dict[str, object]:
    """One position's latest record, or the gap the run's own records prove: only a replay step can be not run."""
    key = position_key(position)
    if key in records:
        return {name: records[key][name] for name in http_routine.RUN_STEP_FIELDS}
    step = position.get("step")
    if (
        step is not None
        and reached is not None
        and (step > reached[0] + 1 or (step == reached[0] + 1 and not reached[1]))
    ):
        return _gap(position, "not_run")
    return _gap(position, "unavailable")


def _page_steps(records, run, totals: tuple[int, int], offset: int) -> list[dict[str, object]]:
    """Whole consecutive positions from ``offset``: replay steps first, then decision calls."""
    replay, total = totals
    reached = None if run is None else (run["reached"], run["dispatched"])
    chosen: list[dict[str, object]] = []
    used = 2
    for index in range(offset + 1, total + 1):
        entry = _entry(records, reached, http_routine.run_position(index, replay))
        cost = http_routine.encoded_bytes(entry) + 1
        if chosen and (len(chosen) == http_routine.MAX_PAGE_STEPS or used + cost > http_routine.MAX_PAGE_BYTES):
            break
        chosen.append(entry)
        used += cost
    return chosen


def run_steps(self, team_id: str, run_id: str, snapshot: str, offset: int, now: int) -> dict[str, object]:
    """A Supervisor's page of one run's entries, bound to its own revision and one snapshot of its records.

    The page needs only the run's own records, never its revision's plan, so it renders after any later change.
    """
    team_id = validate_team_id(team_id)
    incarnation = self.assistant_lifecycle._network(team_id).id
    try:
        token, records, run = self.routine_diagnostics.run_steps(team_id, incarnation, run_id, now, (snapshot, offset))
        binding = _binding_of(records, run)
    except RunChangedError as exc:
        raise ApiProblem(HTTPStatus.CONFLICT, "Routine run records changed", code="routine-run-changed") from exc
    except DiagnosticStoreError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Routine diagnostics are unavailable", code="routine-state-unavailable"
        ) from exc
    total = None if binding is None else binding["total"] + _calls(records, run)
    if total is None or not (offset == 0 or 0 <= offset < total):
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine run steps are unavailable", code="routine-run-steps-not-found")
    steps = _page_steps(records, run, (binding["total"], total), offset)
    following = offset + len(steps)
    view = http_routine.canonical_run_steps(
        {
            "team_id": team_id,
            "run_id": run_id,
            **{key: binding[key] for key in ("routine_id", "revision", "plan_digest")},
            "replay": binding["total"],
            "total": total,
            "snapshot": token,
            "ended": run is not None,
            "offset": offset,
            "steps": steps,
            "next": None if following == total else following,
            "decision": None if run is None else run["decision"],
        }
    )
    if view is None:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Routine diagnostics are unavailable", code="routine-state-unavailable"
        )
    return view
