"""Durability and fencing contracts for Assistant update transactions."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from install.bindings import DynamicAssistantConflictError, DynamicAssistantError, DynamicAssistantStore
from install.contract import CONTRACT_ROOT
from install.update import AssistantUpdateStore

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]


class AssistantUpdateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.bindings = DynamicAssistantStore(root / "bindings.json")
        self.updates = AssistantUpdateStore(root / "updates")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_transaction_is_durable_idempotent_and_exactly_clearable(self) -> None:
        previous = self.bindings.put("team_1", copy.deepcopy(RESOLUTION))
        successor = copy.deepcopy(RESOLUTION)
        successor["assistant_version"] = "0.2.0"
        successor["source_digest"] = f"sha256:{'9' * 64}"

        image_id = f"sha256:{'a' * 64}"
        update = self.updates.begin(previous, successor, image_id)
        repeated = self.updates.begin(previous, successor, image_id)

        self.assertEqual(repeated, update)
        self.assertEqual(self.updates.get("team_1", previous.assistant_id), update)
        self.assertEqual(self.updates.list(), (update,))
        self.updates.clear(update)
        self.updates.clear(update)
        self.assertEqual(self.updates.list(), ())

    def test_conflicting_or_malformed_transaction_fails_closed(self) -> None:
        previous = self.bindings.put("team_1", copy.deepcopy(RESOLUTION))
        first = copy.deepcopy(RESOLUTION)
        first["assistant_version"] = "0.2.0"
        first["source_digest"] = f"sha256:{'8' * 64}"
        second = copy.deepcopy(first)
        second["source_digest"] = f"sha256:{'9' * 64}"
        self.updates.begin(previous, first, f"sha256:{'a' * 64}")

        with self.assertRaises(DynamicAssistantConflictError):
            self.updates.begin(previous, second, f"sha256:{'a' * 64}")

        transaction_path = next((Path(self.directory.name) / "updates").glob("*.json"))
        transaction_path.write_text("{}", encoding="ascii")
        with self.assertRaises(DynamicAssistantError):
            self.updates.list()

    def test_identity_cannot_escape_the_transaction_root(self) -> None:
        with self.assertRaises(DynamicAssistantError):
            self.updates.get("../team", "hello-world")
        with self.assertRaises(DynamicAssistantError):
            self.updates.get("team_1", "../assistant")
