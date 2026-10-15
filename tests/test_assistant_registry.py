"""Shared Assistant registry contracts."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.install import runtime as local_runtime


class SharedRegistry(unittest.TestCase):
    def test_an_all_zero_digest_is_refused(self):
        zero = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "0" * 64
        self.assertFalse(local_runtime.is_digest_ref(zero))
