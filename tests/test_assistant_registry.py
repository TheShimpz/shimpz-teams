"""Shared Assistant registry contracts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant import spec as assistant_registry
from local.install import runtime as local_runtime


class SharedRegistry(unittest.TestCase):
    def test_both_reject_all_zero_digest(self):
        zero = "ghcr.io/theshimpz/shimpz-space@sha256:" + "0" * 64
        self.assertFalse(assistant_registry.is_digest_image(zero))
        self.assertFalse(local_runtime.is_digest_ref(zero))

    def test_real_digest_still_accepted(self):
        image = "ghcr.io/example/example-assistant@sha256:" + ("a" * 64)
        self.assertTrue(assistant_registry.is_digest_image(image))

    def test_local_runtime_reuses_shared_contract_types(self):
        self.assertIs(local_runtime.PowerSpec, assistant_registry.PowerSpec)
        self.assertIs(local_runtime.IntegrationSpec, assistant_registry.IntegrationSpec)
