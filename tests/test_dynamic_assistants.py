"""Contract tests for durable dynamic Assistant bindings."""

import copy
import json
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from install.bindings import (
    AssistantLimitReachedError,
    DynamicAssistantConflictError,
    DynamicAssistantError,
    DynamicAssistantStore,
    binding_from_resolution,
)
from install.contract import CONTRACT_ROOT
from protocol.http.v1 import payload as http_payload

VECTORS = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())
RESOLUTION = VECTORS["fixtures"]["resolve_response"]["value"]
LOCAL_RECORD = {
    "assistant_id": "local-example",
    "image_id": "sha256:" + ("a" * 64),
}


def validate_local_record(record: dict[str, object]) -> None:
    if set(record) != {"assistant_id", "image_id"} or record["assistant_id"] != "local-example":
        raise DynamicAssistantError("the local Assistant record is invalid")


def runtime_resolution() -> dict[str, object]:
    resolution = copy.deepcopy(RESOLUTION)
    action = resolution["machine_contract"]["actions"][0]
    action["input_schema"]["additionalProperties"] = False
    action["output_schema"]["additionalProperties"] = False
    resolution["stored_inputs"] = []
    action["stored_inputs"] = []
    action["human_requests"] = []
    return resolution


def named_resolution(assistant_id: str) -> dict[str, object]:
    resolution = copy.deepcopy(RESOLUTION)
    resolution["assistant_id"] = assistant_id
    return resolution


class TeamAssistantLimitTests(unittest.TestCase):
    """A Team holds at most MAX_TEAM_ASSISTANTS bindings; only a new Assistant id can be refused for it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bindings.json"
        self.store = DynamicAssistantStore(self.path)

    def _fill(self, count: int) -> None:
        for index in range(count):
            self.store.put("team_1", named_resolution(f"helper-{index}"))

    def test_a_new_assistant_beyond_the_limit_is_refused_without_writing(self) -> None:
        self._fill(http_payload.MAX_TEAM_ASSISTANTS)
        before = self.path.read_bytes()

        with self.assertRaises(AssistantLimitReachedError):
            self.store.put("team_1", named_resolution("one-more"))

        self.assertEqual(self.path.read_bytes(), before)
        # The limit is per Team.
        self.store.put("team_2", named_resolution("one-more"))

    def test_every_installed_binding_counts_even_one_the_current_contract_refuses(self) -> None:
        DynamicAssistantStore(self.path, local_record_validator=validate_local_record).put_local(
            "team_1", copy.deepcopy(LOCAL_RECORD)
        )
        self._fill(http_payload.MAX_TEAM_ASSISTANTS - 1)
        (refused,) = [binding for binding in self.store.list("team_1") if not binding.admissible]
        self.assertEqual(refused.assistant_id, "local-example")

        with self.assertRaises(AssistantLimitReachedError):
            self.store.put("team_1", named_resolution("one-more"))

    def test_an_identical_reinstall_or_a_replacement_at_the_limit_is_admitted(self) -> None:
        self._fill(http_payload.MAX_TEAM_ASSISTANTS)
        current = self.store.get("team_1", "helper-0")

        self.assertEqual(self.store.put_with_status("team_1", named_resolution("helper-0")), (current, False))
        successor = named_resolution("helper-0")
        successor["source_digest"] = f"sha256:{'9' * 64}"
        replaced = self.store.replace("team_1", current.binding_digest, successor)

        self.assertEqual(self.store.get("team_1", "helper-0"), replaced)
        self.assertEqual(len(self.store.list("team_1")), http_payload.MAX_TEAM_ASSISTANTS)

    def test_two_installs_racing_for_the_last_slot_admit_exactly_one(self) -> None:
        self._fill(http_payload.MAX_TEAM_ASSISTANTS - 1)
        ready = threading.Barrier(2)

        def install(assistant_id: str) -> str:
            ready.wait(timeout=5)
            try:
                DynamicAssistantStore(self.path).put("team_1", named_resolution(assistant_id))
            except AssistantLimitReachedError:
                return "refused"
            return "installed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = sorted(executor.map(install, ("racer-a", "racer-b")))

        self.assertEqual(outcomes, ["installed", "refused"])
        self.assertEqual(len(self.store.list("team_1")), http_payload.MAX_TEAM_ASSISTANTS)


class DynamicAssistantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bindings.json"
        self.store = DynamicAssistantStore(self.path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_binding_is_durable_scoped_and_digest_stable(self) -> None:
        first = self.store.put("team_1", copy.deepcopy(RESOLUTION))
        repeated = self.store.put("team_1", copy.deepcopy(RESOLUTION))
        other_team = self.store.put("team_2", copy.deepcopy(RESOLUTION))

        self.assertEqual(first, repeated)
        self.assertEqual(first.binding_digest, repeated.binding_digest)
        self.assertNotEqual(first.binding_digest, other_team.binding_digest)
        self.assertEqual(first.provenance, "published")
        self.assertEqual(DynamicAssistantStore(self.path).get("team_1", "hello-world"), first)
        self.assertEqual(self.store.list("team_1"), (first,))
        self.assertEqual(self.store.snapshot(), (first, other_team))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

        document = json.loads(self.path.read_bytes())
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["bindings"][0]["provenance"], "published")
        self.assertIn("resolution", document["bindings"][0])

    def test_local_binding_requires_an_explicit_profile_validator(self) -> None:
        with self.assertRaisesRegex(DynamicAssistantError, "unavailable in this profile"):
            self.store.put_local("team_1", copy.deepcopy(LOCAL_RECORD))
        self.assertFalse(self.path.exists())

        local_store = DynamicAssistantStore(self.path, local_record_validator=validate_local_record)
        binding = local_store.put_local("team_1", copy.deepcopy(LOCAL_RECORD))

        self.assertEqual(binding.provenance, "local")
        self.assertEqual(binding.local_record, LOCAL_RECORD)
        self.assertEqual(local_store.get("team_1", "local-example"), binding)
        with self.assertRaisesRegex(DynamicAssistantError, "not a publication"):
            _ = binding.resolution
        # A profile that cannot admit a local record keeps the intact binding but refuses every use of it.
        (unadmitted,) = DynamicAssistantStore(self.path).snapshot()
        self.assertFalse(unadmitted.admissible)
        self.assertEqual(unadmitted.binding_digest, binding.binding_digest)
        with self.assertRaisesRegex(DynamicAssistantError, "must be replaced"):
            _ = unadmitted.local_record

    def test_binding_identity_and_replacement_never_cross_provenance(self) -> None:
        local_store = DynamicAssistantStore(self.path, local_record_validator=validate_local_record)
        published = copy.deepcopy(RESOLUTION)
        published["assistant_id"] = "local-example"

        current = local_store.put_local("team_1", copy.deepcopy(LOCAL_RECORD))
        with self.assertRaisesRegex(DynamicAssistantConflictError, "already binds"):
            local_store.put("team_1", published)
        with self.assertRaisesRegex(DynamicAssistantConflictError, "provenance"):
            local_store.replace("team_1", current.binding_digest, published)

    def test_different_artifact_for_same_identity_requires_removal(self) -> None:
        self.store.put("team_1", copy.deepcopy(RESOLUTION))
        replacement = copy.deepcopy(RESOLUTION)
        replacement["source_digest"] = f"sha256:{'9' * 64}"

        with self.assertRaises(DynamicAssistantConflictError):
            self.store.put("team_1", replacement)

        self.assertTrue(self.store.delete("team_1", "hello-world"))
        self.assertFalse(self.store.delete("team_1", "hello-world"))
        self.assertEqual(self.store.list("team_1"), ())

    def test_put_reports_ownership_and_conditional_delete_preserves_a_winner(self) -> None:
        binding, created = self.store.put_with_status("team_1", copy.deepcopy(RESOLUTION))
        repeated, repeated_created = self.store.put_with_status("team_1", copy.deepcopy(RESOLUTION))

        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated, binding)
        self.assertFalse(
            self.store.delete_if_matches(
                "team_1",
                "hello-world",
                f"sha256:{'0' * 64}",
            )
        )
        self.assertEqual(self.store.get("team_1", "hello-world"), binding)
        self.assertTrue(self.store.delete_if_matches("team_1", "hello-world", binding.binding_digest))
        self.assertIsNone(self.store.get("team_1", "hello-world"))

        with self.assertRaisesRegex(DynamicAssistantConflictError, "digest is invalid"):
            self.store.delete_if_matches("team_1", "hello-world", "invalid")

    def test_replace_is_atomic_and_fenced_by_the_previous_binding_digest(self) -> None:
        previous = self.store.put("team_1", copy.deepcopy(RESOLUTION))
        replacement = copy.deepcopy(RESOLUTION)
        replacement["source_digest"] = f"sha256:{'9' * 64}"

        current = self.store.replace("team_1", previous.binding_digest, replacement)

        self.assertEqual(self.store.get("team_1", "hello-world"), current)
        self.assertNotEqual(current.binding_digest, previous.binding_digest)
        with self.assertRaises(DynamicAssistantConflictError):
            self.store.replace("team_1", previous.binding_digest, copy.deepcopy(RESOLUTION))
        self.assertEqual(self.store.get("team_1", "hello-world"), current)

    def test_rejects_invalid_resolution_before_writing(self) -> None:
        invalid = copy.deepcopy(RESOLUTION)
        invalid["image_reference"] = "ghcr.io/attacker/assistant@sha256:" + "b" * 64

        with self.assertRaises(DynamicAssistantError):
            self.store.put("team_1", invalid)

        self.assertFalse(self.path.exists())

    def test_corruption_and_digest_tampering_fail_closed(self) -> None:
        self.path.write_text("{", encoding="ascii")
        with self.assertRaises(DynamicAssistantError):
            self.store.list("team_1")

        self.path.unlink()
        self.store.put("team_1", copy.deepcopy(RESOLUTION))
        document = json.loads(self.path.read_bytes())
        document["bindings"][0]["binding_digest"] = f"sha256:{'0' * 64}"
        self.path.write_text(json.dumps(document), encoding="ascii")
        with self.assertRaises(DynamicAssistantError):
            self.store.list("team_1")

    def test_identifiers_are_closed_ascii_contracts(self) -> None:
        for team_id in ("../team", "téam", ""):
            with self.subTest(team_id=team_id), self.assertRaises(DynamicAssistantError):
                self.store.list(team_id)
        for assistant_id in ("Hello", "../hello", "héllo"):
            with self.subTest(assistant_id=assistant_id), self.assertRaises(DynamicAssistantError):
                self.store.get("team_1", assistant_id)

    def test_reserved_service_alias_is_refused_before_write_and_revalidation(self) -> None:
        resolution = runtime_resolution()
        resolution["assistant_id"] = "postgres"

        with self.assertRaises(DynamicAssistantError):
            self.store.put("team_1", resolution)
        self.assertFalse(self.path.exists())
        with self.assertRaises(DynamicAssistantError):
            binding_from_resolution("team_1", resolution)

    def test_registry_readers_share_the_file_lock(self) -> None:
        expected = self.store.put("team_1", runtime_resolution())
        original_read = self.store._read
        readers_entered = threading.Barrier(2)

        def overlapping_read():
            readers_entered.wait(timeout=2)
            return original_read()

        with (
            mock.patch.object(self.store, "_read", side_effect=overlapping_read),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = tuple(executor.map(lambda _index: self.store.get("team_1", "hello-world"), range(2)))

        self.assertEqual(results, (expected, expected))

    def test_registry_writer_waits_for_shared_reader(self) -> None:
        self.store.put("team_1", runtime_resolution())
        reader_entered = threading.Event()
        release_reader = threading.Event()
        writer_read = threading.Event()
        original_read = self.store._read

        def hold_reader() -> None:
            with self.store._shared_lock():
                reader_entered.set()
                release_reader.wait(timeout=2)

        def observe_writer_read():
            writer_read.set()
            return original_read()

        with ThreadPoolExecutor(max_workers=2) as executor:
            reader = executor.submit(hold_reader)
            self.assertTrue(reader_entered.wait(timeout=1))
            with mock.patch.object(self.store, "_read", side_effect=observe_writer_read):
                writer = executor.submit(self.store.delete, "team_1", "hello-world")
                self.assertFalse(writer_read.wait(timeout=0.1))
                release_reader.set()
                reader.result(timeout=1)
                self.assertTrue(writer.result(timeout=1))


if __name__ == "__main__":
    unittest.main()
