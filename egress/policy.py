"""Private, canonical egress-policy storage shared by both Team profiles.

Each Assistant's policy is a Team-held token and the reviewed hosts the Assistant egress proxy admits for it. Team
presents the token for the Assistant's provider calls; no workload ever receives it (ADR-0106).
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from assistant import manifest as assistant_manifest
from storage import private_state

MAX_POLICY_BYTES = 16 * 1024
TOKEN_FILE_BYTES = 33
_TOKEN = re.compile(r"[0-9a-f]{32}")


class EgressPolicyError(RuntimeError):
    """Base class for a policy store that cannot prove its contract."""


class EgressPolicyUnavailableError(EgressPolicyError):
    """The policy store could not complete an otherwise valid operation."""


class EgressPolicyDriftError(EgressPolicyError):
    """Persisted policy state no longer satisfies its ownership contract."""


def environment_map(raw: object) -> dict[str, str] | None:
    """Parse Docker's ``KEY=value`` list while rejecting ambiguous duplicates."""
    if not isinstance(raw, list) or not all(isinstance(item, str) and "=" in item for item in raw):
        return None
    environment: dict[str, str] = {}
    for item in raw:
        key, value = item.split("=", 1)
        if not key or key in environment:
            return None
        environment[key] = value
    return environment


def _read_exact_private_file(
    path: Path,
    *,
    mode: int,
    group: int | None,
    minimum_bytes: int,
    maximum_bytes: int,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or (group is not None and metadata.st_gid != group)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or not minimum_bytes <= metadata.st_size <= maximum_bytes
        ):
            raise EgressPolicyDriftError("egress policy file metadata drifted")
        raw = bytearray()
        while len(raw) < metadata.st_size:
            chunk = os.read(descriptor, min(4096, metadata.st_size - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise EgressPolicyDriftError("egress policy file changed while it was read")
        return bytes(raw)
    except EgressPolicyDriftError:
        raise
    except OSError as exc:
        raise EgressPolicyDriftError("egress policy file is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class EgressPolicyStore:
    root: Path
    policy_gid: int

    def _require_root(self) -> Path:
        try:
            metadata = self.root.stat(follow_symlinks=False)
        except OSError as exc:
            raise EgressPolicyUnavailableError("egress policy storage is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_gid != self.policy_gid
            or stat.S_IMODE(metadata.st_mode) != 0o750
        ):
            raise EgressPolicyDriftError("egress policy storage metadata drifted")
        return self.root

    @staticmethod
    def _key(identity: str) -> str:
        if not isinstance(identity, str) or not identity:
            raise EgressPolicyDriftError("egress policy identity is invalid")
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _token_path(self, identity: str) -> Path:
        token_dir = self._require_root() / ".tokens"
        try:
            token_dir.mkdir(mode=0o700, exist_ok=True)
            metadata = token_dir.stat(follow_symlinks=False)
        except OSError as exc:
            raise EgressPolicyUnavailableError("egress token storage is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise EgressPolicyDriftError("egress token storage metadata drifted")
        return token_dir / f"{self._key(identity)}.token"

    @staticmethod
    def _read_token(path: Path) -> str:
        raw = _read_exact_private_file(
            path,
            mode=0o600,
            group=None,
            minimum_bytes=TOKEN_FILE_BYTES,
            maximum_bytes=TOKEN_FILE_BYTES,
        )
        try:
            token = raw[:-1].decode("ascii")
        except UnicodeError as exc:
            raise EgressPolicyDriftError("egress token is not canonical") from exc
        if raw[-1:] != b"\n" or _TOKEN.fullmatch(token) is None:
            raise EgressPolicyDriftError("egress token is not canonical")
        return token

    def token(self, identity: str, *, create: bool) -> str | None:
        path = self._token_path(identity)
        try:
            path.stat(follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                return None
        except OSError as exc:
            raise EgressPolicyDriftError("egress token metadata is unavailable") from exc
        else:
            return self._read_token(path)
        token = secrets.token_hex(16)
        try:
            private_state.replace_durably(path, f"{token}\n".encode("ascii"))
        except OSError as exc:
            raise EgressPolicyUnavailableError("egress token could not be saved") from exc
        return self._read_token(path)

    @staticmethod
    def _canonical_hosts(hosts: tuple[str, ...]) -> tuple[tuple[str, ...], bytes]:
        try:
            canonical = assistant_manifest.canonical_allowed_hosts(list(hosts))
        except assistant_manifest.ManifestError as exc:
            raise EgressPolicyDriftError("egress policy hosts are invalid") from exc
        if not canonical or canonical != hosts:
            raise EgressPolicyDriftError("egress policy hosts are not canonical")
        return canonical, json.dumps(list(canonical), separators=(",", ":")).encode("ascii")

    def write(self, token: str, hosts: tuple[str, ...]) -> None:
        if _TOKEN.fullmatch(token) is None:
            raise EgressPolicyDriftError("egress token is invalid")
        _canonical, encoded = self._canonical_hosts(hosts)
        policy_path = self._require_root() / f"{token}.json"
        try:
            private_state.replace_durably(policy_path, encoded, mode=0o640, group=self.policy_gid)
            metadata = policy_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise EgressPolicyUnavailableError("egress policy could not be saved") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != self.policy_gid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o640
        ):
            raise EgressPolicyDriftError("egress policy metadata drifted")

    def admitted(self, identity: str) -> tuple[str, tuple[str, ...]] | None:
        token = self.token(identity, create=False)
        if token is None:
            return None
        raw = _read_exact_private_file(
            self._require_root() / f"{token}.json",
            mode=0o640,
            group=self.policy_gid,
            minimum_bytes=1,
            maximum_bytes=MAX_POLICY_BYTES,
        )
        try:
            hosts = assistant_manifest.canonical_allowed_hosts(json.loads(raw))
            canonical = json.dumps(list(hosts), separators=(",", ":")).encode("ascii")
        except (UnicodeError, json.JSONDecodeError, RecursionError, assistant_manifest.ManifestError) as exc:
            raise EgressPolicyDriftError("egress policy content is invalid") from exc
        if not hosts or not hmac.compare_digest(raw, canonical):
            raise EgressPolicyDriftError("egress policy content is not canonical")
        return token, hosts

    def validate(self, identity: str, hosts: tuple[str, ...]) -> str:
        return self.validate_admitted(self.admitted(identity), hosts)

    def validate_admitted(
        self,
        admitted: tuple[str, tuple[str, ...]] | None,
        hosts: tuple[str, ...],
    ) -> str:
        expected_hosts, _encoded = self._canonical_hosts(hosts)
        if admitted is None:
            raise EgressPolicyDriftError("egress policy is missing")
        token, actual_hosts = admitted
        if not hmac.compare_digest(
            json.dumps(list(actual_hosts), separators=(",", ":")),
            json.dumps(list(expected_hosts), separators=(",", ":")),
        ):
            raise EgressPolicyDriftError("egress policy hosts drifted")
        return token

    def remove(self, identity: str) -> None:
        token_path = self._token_path(identity)
        try:
            token_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise EgressPolicyUnavailableError("egress token metadata is unavailable") from exc
        token = self._read_token(token_path)
        policy_path = self._require_root() / f"{token}.json"
        try:
            try:
                metadata = policy_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                metadata = None
            if metadata is not None:
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_gid != self.policy_gid
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o640
                ):
                    raise EgressPolicyDriftError("egress policy metadata drifted")
                policy_path.unlink()
            token_path.unlink()
        except EgressPolicyDriftError:
            raise
        except OSError as exc:
            raise EgressPolicyUnavailableError("egress policy could not be removed") from exc
