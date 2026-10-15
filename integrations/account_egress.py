"""Fail-closed reader for the Account egress machine capability."""

import grp
import os
import re
from pathlib import Path

from storage import private_state

CAPABILITY_PATH = Path("/run/shimpz-account-egress/token")
CAPABILITY_GROUP = "shimpzaccount-egress-token"
# The non-root Account egress capability initializer is the writer of record; Team reads only through group 10022.
CAPABILITY_OWNER_UID = 10023
_CAPABILITY = re.compile(r"[0-9a-f]{64}\Z")


class AccountEgressCapabilityError(OSError):
    """The Account egress capability is unavailable or has unsafe custody."""


def _identity(metadata: os.stat_result, expected_gid: int) -> tuple[int, int, int, int, int]:
    if not private_state.file_custody_holds(
        metadata, uid=CAPABILITY_OWNER_UID, gid=expected_gid, mode=0o440, size=(64, 64)
    ):
        raise AccountEgressCapabilityError("Account egress capability metadata is unsafe")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def read_capability() -> str:
    """Read the exact capability for each outbound broker operation."""
    descriptor = -1
    try:
        expected_gid = grp.getgrnam(CAPABILITY_GROUP).gr_gid
        descriptor = os.open(CAPABILITY_PATH, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        identity = _identity(before, expected_gid)
        raw = os.read(descriptor, 65)
        after = os.fstat(descriptor)
    except (KeyError, OSError) as exc:
        raise AccountEgressCapabilityError("Account egress capability is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _identity(after, expected_gid) != identity or len(raw) != 64:
        raise AccountEgressCapabilityError("Account egress capability changed while reading")
    try:
        capability = raw.decode("ascii")
    except UnicodeError as exc:
        raise AccountEgressCapabilityError("Account egress capability is invalid") from exc
    if _CAPABILITY.fullmatch(capability) is None:
        raise AccountEgressCapabilityError("Account egress capability is invalid")
    return capability
