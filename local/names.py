"""Local Team display names (ADR-0088): list, create, and rename Teams by a mutable name over an immutable id.

A Team's network label keeps its creation name and proves ownership. A rename writes a Team-owned record, bound to
the network incarnation, in the private inference volume; the current name is that record, or the label when no record
exists. Names are unique per Space ignoring case, under one Space-wide namespace lock taken before any Team lock.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

from docker.errors import APIError

from core import strict_json
from inference import config as inference_config
from local.errors import ApiProblemError as ApiProblem
from local.errors import ownership_conflict
from local.labels import TEAM_LABEL, TEAM_NAME_LABEL
from local.validation import validate_team_id
from protocol.http.v1 import payload as http_payload
from storage import files as team_storage

SCHEMA = 1
MAX_RECORD_BYTES = 4096
# Every name this store writes: a Team's display-name record and its interrupted temporary file.
_OWNED_NAME_RE = re.compile(r"(?:[0-9a-f]{64}\.name\.json|\.[0-9a-f]{64}\.name\.json\.[0-9a-f]{16}\.tmp)\Z")
_RECORD_KEYS = {"schema", "team_id", "network_id", "team_name"}
# Docker's RFC 3339 network creation time: whole seconds, an optional fraction of up to nanoseconds, and an offset
# bounded here because datetime.fromisoformat would normalize an out-of-range one such as +00:60.
_CREATED_RE = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?"
    r"(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _unavailable() -> ApiProblem:
    return ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Team names failed their safety checks",
        code="team-names-unavailable",
    )


def canonical_name(value: object) -> str:
    """A Local display name as sent: 1 to 80 trimmed characters without controls, already NFC."""
    name = http_payload.canonical_local_team_name(value)
    if name is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Team name must contain 1 to 80 trimmed characters",
            code="invalid-team-name",
        )
    return name


def _folded(name: str) -> str:
    return unicodedata.normalize("NFC", name.casefold())


class TeamNameStore:
    """One record per renamed Team; an absent record means the Team still has its creation name."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _digest(team_id: str) -> str:
        return hashlib.sha256(team_id.encode("ascii")).hexdigest()

    def _path(self, team_id: str) -> Path:
        return self.root / f"{self._digest(team_id)}.name.json"

    def load(self, team_id: str, network_id: str) -> str | None:
        """The recorded name of this exact incarnation; a record that is unsafe or not its own fails closed."""
        try:
            # No link is followed and a special file never blocks the read; only a regular file is a record.
            descriptor = os.open(self._path(team_id), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _unavailable() from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise _unavailable()
            with os.fdopen(descriptor, "rb") as stream:
                raw = stream.read(MAX_RECORD_BYTES + 1)
            value = strict_json.loads(raw)
        except (OSError, ValueError) as exc:
            raise _unavailable() from exc
        if (
            len(raw) > MAX_RECORD_BYTES
            or not isinstance(value, dict)
            or set(value) != _RECORD_KEYS
            or type(value["schema"]) is not int
            or value["schema"] != SCHEMA
            or value["team_id"] != team_id
            or value["network_id"] != network_id
        ):
            raise _unavailable()
        name = http_payload.canonical_local_team_name(value["team_name"])
        if name is None:
            raise _unavailable()
        return name

    def save(self, team_id: str, network_id: str, team_name: str) -> None:
        record = {"schema": SCHEMA, "team_id": team_id, "network_id": network_id, "team_name": team_name}
        try:
            inference_config.write_private_json(self.root, self._path(team_id), record)
        except OSError as exc:
            raise _unavailable() from exc

    def _owned(self) -> list[Path]:
        try:
            return [path for path in self.root.iterdir() if _OWNED_NAME_RE.fullmatch(path.name)]
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise _unavailable() from exc

    def _unlink(self, paths: list[Path]) -> None:
        try:
            for path in paths:
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise _unavailable() from exc

    def delete(self, team_id: str) -> None:
        """Remove the Team's record and any temporary file an interrupted rename left."""
        record = self._path(team_id)
        temporary = f".{record.name}."
        self._unlink([record, *(path for path in self._owned() if path.name.startswith(temporary))])

    def delete_all(self) -> None:
        """Remove every record, including one whose Team network a crash already removed."""
        self._unlink(self._owned())


def display_name(self, team_id: str, network) -> str:
    """The Team's current name: its record for this network, or the creation label; ownership is revalidated."""
    label = self.assistant_lifecycle._validate_network(network, team_id, refresh=False)
    return self.team_names.load(team_id, network.id) or label


def _created_ns(network) -> int:
    """The Team network's Docker creation instant in nanoseconds since the epoch, whatever offset Docker reports."""
    created = network.attrs.get("Created")
    match = _CREATED_RE.fullmatch(created) if isinstance(created, str) else None
    try:
        if match is None:
            raise ValueError("creation time is not RFC 3339")
        whole, fraction, offset = match.groups()
        instant = datetime.fromisoformat(whole + ("+00:00" if offset == "Z" else offset))
    except ValueError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker returned an invalid Team creation time",
            code="team-metadata-invalid",
        ) from exc
    return (instant - _EPOCH) // timedelta(seconds=1) * 10**9 + int((fraction or "").ljust(9, "0"))


def _named_teams(self) -> list[tuple[str, str, object]]:
    teams: list[tuple[str, str, object]] = []
    for network in self.assistant_lifecycle._managed_team_networks():
        team_id = (network.attrs.get("Labels") or {}).get(TEAM_LABEL)
        if not isinstance(team_id, str):
            raise ownership_conflict()
        validate_team_id(team_id)
        teams.append((team_id, display_name(self, team_id, network), network))
    return teams


def _require_free(self, team_name: str, team_id: str) -> None:
    folded = _folded(team_name)
    if any(other != team_id and _folded(name) == folded for other, name, _network in _named_teams(self)):
        raise ApiProblem(HTTPStatus.CONFLICT, "another Team already has this name", code="team-name-taken")


def list_teams(self) -> dict[str, list[dict[str, str]]]:
    """Every Team, newest first by its network's creation instant; the id orders only equal instants."""
    with self._names_lock:
        named = [(-_created_ns(network), team_id, name) for team_id, name, network in _named_teams(self)]
    named.sort()
    return {"teams": [{"team_id": team_id, "team_name": name, "status": "running"} for _key, team_id, name in named]}


def _existing(self, team_id: str, team_name: str, network) -> dict[str, object]:
    if display_name(self, team_id, network) != team_name:
        raise ApiProblem(HTTPStatus.CONFLICT, "Team id already belongs to a different name", code="team-name-conflict")
    return {"team_id": team_id, "team_name": team_name, "status": "running", "created": False}


def create_team(self, team_id: str, team_name: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    team_name = canonical_name(team_name)
    with self._names_lock, self._lock(team_id):
        existing = self.assistant_lifecycle._network(team_id, required=False)
        if existing is not None:
            return _existing(self, team_id, team_name, existing)
        _require_free(self, team_name, team_id)
        try:
            # A Team identity starts empty even after a daemon crash removed its network
            # before the previous lifecycle could clean the dedicated storage volume.
            self.storage.destroy(team_id)
        except team_storage.StorageError as exc:
            self._raise_storage_problem(exc)
        try:
            self.inference_store.delete(team_id)
        except inference_config.InferenceConfigError as exc:
            self._raise_inference_problem(exc)
        self.team_names.delete(team_id)
        # Nothing a previous incarnation's person sent is ever cited by this one.
        self.routine_recent.drop(team_id)
        try:
            labels = self.assistant_lifecycle._base_labels(team_id, "team")
            labels[TEAM_NAME_LABEL] = team_name
            network = self.client.networks.create(
                self.assistant_lifecycle._network_name(team_id),
                driver="bridge",
                internal=True,
                attachable=False,
                check_duplicate=True,
                labels=labels,
            )
        except APIError as exc:
            # A concurrent idempotent creator is safe only when the resulting
            # resource proves the exact ownership/profile labels.
            network = self.assistant_lifecycle._network(team_id, required=False)
            if network is None:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Docker could not create the Team",
                    code="docker-create-failed",
                ) from exc
            return _existing(self, team_id, team_name, network)
        self.assistant_lifecycle._validate_network(network, team_id, refresh=False)
        return {"team_id": team_id, "team_name": team_name, "status": "running", "created": True}


def rename_team(self, team_id: str, team_name: str) -> dict[str, str]:
    team_id = validate_team_id(team_id)
    team_name = canonical_name(team_name)
    with self._names_lock, self._lock(team_id):
        network = self.assistant_lifecycle._network(team_id)
        if display_name(self, team_id, network) != team_name:
            _require_free(self, team_name, team_id)
            if team_name == self.assistant_lifecycle._validate_network(network, team_id, refresh=False):
                self.team_names.delete(team_id)
            else:
                self.team_names.save(team_id, network.id, team_name)
        return {"team_id": team_id, "team_name": team_name}
