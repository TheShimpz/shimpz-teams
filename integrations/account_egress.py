"""Fail-closed reader for the Account egress machine capability."""

from __future__ import annotations

import grp
import os
import re
import stat
from pathlib import Path

CAPABILITY_PATH = Path("/run/shimpz-account-egress/token")
CAPABILITY_GROUP = "shimpzaccount-egress-token"
_CAPABILITY = re.compile(r"[0-9a-f]{64}\Z")


class AccountEgressCapabilityError(OSError):
    """The Account egress capability is unavailable or has unsafe custody."""


def _identity(
    metadata: os.stat_result,
    expected_gid: int,
    owner_uid: int,
) -> tuple[int, int, int, int, int]:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o440
        or metadata.st_uid != owner_uid
        or metadata.st_gid != expected_gid
        or metadata.st_size != 64
    ):
        raise AccountEgressCapabilityError("Account egress capability metadata is unsafe")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def read_capability(
    path: Path = CAPABILITY_PATH,
    *,
    group: str = CAPABILITY_GROUP,
    owner_uid: int = 0,
) -> str:
    """Read the exact capability for each outbound broker operation."""
    descriptor = -1
    try:
        expected_gid = grp.getgrnam(group).gr_gid
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        identity = _identity(before, expected_gid, owner_uid)
        raw = os.read(descriptor, 65)
        after = os.fstat(descriptor)
    except (KeyError, OSError) as exc:
        raise AccountEgressCapabilityError("Account egress capability is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _identity(after, expected_gid, owner_uid) != identity or len(raw) != 64:
        raise AccountEgressCapabilityError("Account egress capability changed while reading")
    try:
        capability = raw.decode("ascii")
    except UnicodeError as exc:
        raise AccountEgressCapabilityError("Account egress capability is invalid") from exc
    if _CAPABILITY.fullmatch(capability) is None:
        raise AccountEgressCapabilityError("Account egress capability is invalid")
    return capability
