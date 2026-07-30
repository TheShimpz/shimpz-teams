"""Shared Team resource parser contracts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.container import network as network_policy
from hosted import container as container_spec


class SingleMemoryParser(unittest.TestCase):
    def test_single_parser_object(self):
        self.assertIs(container_spec.hard_memory_bytes, network_policy.hard_memory_bytes)

    def test_parses_identically(self):
        for value in ("64m", "1g", "512k", "2gib", "1048576", 1024):
            self.assertEqual(
                container_spec.hard_memory_bytes(value, setting="x"),
                network_policy.hard_memory_bytes(value, setting="x"),
            )

    def test_rejects_bool_and_accepts_int(self):
        with self.assertRaises(ValueError):
            network_policy.hard_memory_bytes(True, setting="x")
        self.assertEqual(network_policy.hard_memory_bytes(1024, setting="x"), 1024)

    def test_envelope_and_log_agree(self):
        self.assertEqual(container_spec.MEM_LIMIT_BYTES, network_policy.BRAIN_MEMORY_BYTES)
        self.assertEqual(container_spec.ASSISTANT_MEM_LIMIT_BYTES, network_policy.ASSISTANT_MEMORY_BYTES)
        self.assertEqual(container_spec.TEAM_LOG_MAX_SIZE, network_policy.TEAM_LOG_MAX_SIZE)
        self.assertEqual(container_spec.TEAM_LOG_MAX_FILE, network_policy.TEAM_LOG_MAX_FILE)
