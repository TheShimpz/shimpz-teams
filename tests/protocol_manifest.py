"""Prove a protocol directory's contract-files.sha256 covers and digests every file beside it."""

import hashlib
import re
import unittest
from pathlib import Path

ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


def assert_manifest_holds(case: unittest.TestCase, directory: Path) -> None:
    """Every row is canonical, names exactly the directory's other entries, and digests each one's bytes."""
    manifest = directory / "contract-files.sha256"
    matches = [ROW.fullmatch(line) for line in manifest.read_text(encoding="ascii").splitlines()]
    case.assertTrue(matches)
    case.assertTrue(all(matches))
    expected = {match[2]: match[1] for match in matches if match is not None}
    actual = {path.name for path in directory.iterdir() if path != manifest and path.name != "__pycache__"}
    case.assertEqual(set(expected), actual)
    for filename, digest in expected.items():
        case.assertEqual(hashlib.sha256((directory / filename).read_bytes()).hexdigest(), digest)
