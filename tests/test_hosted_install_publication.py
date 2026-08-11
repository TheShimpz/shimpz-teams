from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from assistant import manifest as assistant_manifest
from hosted.install import publication
from install import bindings
from install.contract import CONTRACT_ROOT

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]


class HostedInstallPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        publication._cached_assistant_spec.cache_clear()

    def test_icon_lifecycle_uses_exact_publication_digests(self) -> None:
        client = SimpleNamespace(icon=mock.Mock(return_value=b"png"))
        store = SimpleNamespace(put=mock.Mock(), discard_unreferenced=mock.Mock())
        binding_store = SimpleNamespace(snapshot=mock.Mock(return_value=("binding",)))

        publication.retain_icon(client, store, RESOLUTION)
        client.icon.assert_called_once_with(RESOLUTION["source_digest"], RESOLUTION["icon_digest"])
        store.put.assert_called_once_with(RESOLUTION, b"png")

        publication.discard_icon(store, binding_store, RESOLUTION["source_digest"])
        store.discard_unreferenced.assert_called_once_with(RESOLUTION["source_digest"], ("binding",))

    def test_spec_rejects_noncanonical_and_malformed_resolutions(self) -> None:
        resolution = copy.deepcopy(RESOLUTION)
        with (
            mock.patch.object(
                assistant_manifest,
                "canonical_machine_contract",
                return_value={"actions": []},
            ),
            self.assertRaises(bindings.DynamicAssistantError),
        ):
            publication._build_assistant_spec(resolution["assistant_id"], resolution)

        with self.assertRaises(bindings.DynamicAssistantError):
            publication._build_assistant_spec("assistant", {})
        with self.assertRaises(bindings.DynamicAssistantError):
            publication._cached_assistant_spec("digest", b"not-json")

        resolution["assistant_id"] = 7
        with self.assertRaises(bindings.DynamicAssistantError):
            publication._cached_assistant_spec("digest", json.dumps(resolution).encode())

    def test_spec_retains_the_reviewed_assistant_name(self) -> None:
        with mock.patch.object(
            assistant_manifest,
            "canonical_machine_contract",
            return_value=RESOLUTION["machine_contract"],
        ):
            spec = publication._build_assistant_spec(RESOLUTION["assistant_id"], RESOLUTION)

        self.assertEqual(spec.contract.name, RESOLUTION["name"])

    def test_binding_digest_is_recomputed_before_cache_use(self) -> None:
        invalid = SimpleNamespace(
            team_id="team_1",
            resolution=copy.deepcopy(RESOLUTION),
            binding_digest="sha256:" + "0" * 64,
        )
        with self.assertRaises(bindings.DynamicAssistantError):
            publication.assistant_spec(invalid)


if __name__ == "__main__":
    unittest.main()
