"""Shared Assistant registry contracts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant_human import assistant_registry, marketplace
from local.install import runtime as local_runtime


class SharedRegistry(unittest.TestCase):
    def test_both_reject_all_zero_digest(self):
        zero = "ghcr.io/theshimpz/shimpz-space@sha256:" + "0" * 64
        self.assertFalse(marketplace.is_digest_image(zero))
        self.assertFalse(local_runtime.is_digest_ref(zero))

    def test_real_digest_still_accepted(self):
        image = "ghcr.io/example/example-assistant@sha256:" + ("a" * 64)
        self.assertTrue(marketplace.is_digest_image(image))

    def test_single_dataclass_and_validators(self):
        self.assertIs(marketplace.PowerSpec, assistant_registry.PowerSpec)
        self.assertIs(local_runtime.PowerSpec, assistant_registry.PowerSpec)
        self.assertIs(marketplace.AccountSpec, assistant_registry.AccountSpec)
        self.assertIs(local_runtime.AccountSpec, assistant_registry.AccountSpec)
