from __future__ import annotations

import copy
import json
import types
import unittest
from unittest import mock

from test_local_chat_continuations import pending

from integrations import challenges as integration_challenges
from local.chat import continuation as continuation
from local.chat import continuation_store
from power import human as power_human


class ContinuationCodecPrimitiveEdgeTests(unittest.TestCase):
    def test_closed_primitives_reject_shape_text_and_identifier_drift(self) -> None:
        invalid = (
            (continuation._mapping, ([], set(), "mapping")),
            (continuation._mapping, ({"extra": True}, set(), "mapping")),
            (continuation._sequence, ((), 1, "sequence")),
            (continuation._sequence, ([1, 2], 1, "sequence")),
            (continuation._text, (None, 8, "text")),
            (continuation._text, (" padded ", 20, "text")),
            (continuation._component_id, ("INVALID ID", "component")),
            (continuation._interrupt_id, ("invalid id",)),
        )
        for operation, arguments in invalid:
            with (
                self.subTest(operation=operation.__name__, arguments=arguments),
                self.assertRaises(continuation.ContinuationCodecError),
            ):
                operation(*arguments)
        self.assertIsNone(continuation._text(None, 8, "text", optional=True))

    def test_json_value_enforces_depth_nodes_numbers_keys_and_types(self) -> None:
        self.assertEqual(continuation._json_value(1.5), 1.5)
        nested: object = None
        for _index in range(continuation.MAX_JSON_DEPTH + 1):
            nested = [nested]
        invalid_values = (
            nested,
            [None] * (continuation.MAX_JSON_NODES + 1),
            float("inf"),
            {1: "value"},
            {"x" * 129: "value"},
            object(),
        )
        for value in invalid_values:
            with self.subTest(value_type=type(value)), self.assertRaises(continuation.ContinuationCodecError):
                continuation._json_value(value)

    def test_pending_transcript_identity_and_requirement_shapes_are_closed(self) -> None:
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._pending_payload(object())
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._transcripts_payload([])
        duplicate = power_human.PowerTranscript("interrupt")
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._transcripts_payload((duplicate, duplicate))

        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._identity_payload(())
        bad_identity = list(pending().identity)
        bad_identity[4] = object()
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._identity_payload(tuple(bad_identity))
        bad_identity = list(pending().identity)
        bad_identity[2] = []
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._identity_payload(tuple(bad_identity))

        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._requirements_payload("human", ())
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._requirements_payload("unknown", (object(),))


class ContinuationCodecBindingEdgeTests(unittest.TestCase):
    def test_release_images_and_bindings_reject_missing_or_malformed_authority(self) -> None:
        state = pending()
        identity = list(state.identity)
        identity[2] = ("malformed",)
        malformed = continuation.PendingLocalChat(
            state.continuation,
            state.assistant_ids,
            state.file_ids,
            state.provider,
            tuple(identity),
        )
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._release_images(malformed)

        identity[2] = (("demo-assistant", "not-a-digest", "container"),)
        malformed = continuation.PendingLocalChat(
            state.continuation,
            state.assistant_ids,
            state.file_ids,
            state.provider,
            tuple(identity),
        )
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._release_images(malformed)

        integration = integration_challenges.IntegrationRequirement(
            "missing-assistant",
            "Missing",
            ("power",),
            (("integration", "provider", ("scope",)),),
        )
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._bindings("integrations", (integration,), state)

        human = types.SimpleNamespace(
            assistant_id="demo-assistant",
            power_id="publish",
            request=object(),
        )
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._bindings("human", (human,), state)
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._bindings("unknown", (object(),), state)

    def test_encode_maps_serializer_and_fixed_size_failures(self) -> None:
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant",
                "Demo Assistant",
                ("publish",),
                (("cloudflare", "cloudflare", ("zone.read",)),),
            ),
        )
        with (
            mock.patch.object(
                continuation.json,
                "dumps",
                side_effect=TypeError("unencodable"),
            ),
            self.assertRaisesRegex(
                continuation.ContinuationCodecError,
                "could not be encoded",
            ),
        ):
            continuation.encode("integrations", requirements, pending())

        with (
            mock.patch.object(continuation.json, "dumps", return_value="x" * 8),
            mock.patch.object(
                continuation.local_chat_continuation_store,
                "MAX_PLAINTEXT_BYTES",
                1,
            ),
            self.assertRaisesRegex(
                continuation.ContinuationCodecError,
                "fixed byte limit",
            ),
        ):
            continuation.encode("integrations", requirements, pending())


class ContinuationCodecDecodeEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_pending = continuation._pending_payload(pending())

    def test_payload_power_and_brain_continuation_reject_drift(self) -> None:
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._decode_payload(b"\xff")

        request = {
            "interrupt_id": "interrupt",
            "assistant_id": "assistant",
            "power": "power",
            "input": [],
        }
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._power_request(request)

        raw = copy.deepcopy(self.raw_pending["continuation"])
        raw["seen_interrupts"] = ["duplicate", "duplicate"]
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._continuation(raw)

        raw = copy.deepcopy(self.raw_pending["continuation"])
        raw["round_index"] = True
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._continuation(raw)

    def test_identity_rejects_network_assistant_file_and_inference_drift(self) -> None:
        base = self.raw_pending["identity"]
        mutations = []
        value = copy.deepcopy(base)
        value["network_id"] = "invalid id"
        mutations.append(value)
        value = copy.deepcopy(base)
        value["assistants"] = [["malformed"]]
        mutations.append(value)
        value = copy.deepcopy(base)
        value["assistants"][0][1] = "not-a-digest"
        mutations.append(value)
        value = copy.deepcopy(base)
        value["assistants"].append(copy.deepcopy(value["assistants"][0]))
        mutations.append(value)
        value = copy.deepcopy(base)
        value["files"][0]["id"] = "bad"
        mutations.append(value)
        value = copy.deepcopy(base)
        value["files"].append(copy.deepcopy(value["files"][0]))
        mutations.append(value)
        value = copy.deepcopy(base)
        value["inference"] = {"provider": "unknown", "model": "model"}
        mutations.append(value)

        for value in mutations:
            with self.subTest(value=value), self.assertRaises(continuation.ContinuationCodecError):
                continuation._identity(value)

    def test_pending_rejects_duplicate_selection_and_provider_drift(self) -> None:
        mutations = []
        value = copy.deepcopy(self.raw_pending)
        value["assistant_ids"] = ["demo-assistant", "demo-assistant"]
        mutations.append(value)
        value = copy.deepcopy(self.raw_pending)
        value["file_ids"] = ["bad"]
        mutations.append(value)
        value = copy.deepcopy(self.raw_pending)
        value["provider"] = "unknown"
        mutations.append(value)
        value = copy.deepcopy(self.raw_pending)
        value["provider"] = "anthropic"
        mutations.append(value)

        for value in mutations:
            with self.subTest(provider=value.get("provider")), self.assertRaises(continuation.ContinuationCodecError):
                continuation._pending(value)

    def test_human_transcript_and_requirement_decoders_are_closed(self) -> None:
        invalid_response = {
            "kind": "input:password",
            "ordinal": 0,
            "fingerprint": "a" * 64,
            "value": "secret",
        }
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._human_response(invalid_response, 0)

        transcript = {
            "interrupt_id": "duplicate",
            "responses": [],
        }
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._transcripts([transcript, transcript])

        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._tuple_text([], 10, "values")

        invalid_integrations = {
            "assistant_id": "assistant",
            "assistant_name": "Assistant",
            "power_ids": ["power"],
            "integrations": [["malformed"]],
        }
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._integration_requirement(invalid_integrations)
        invalid_integrations["integrations"] = []
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._integration_requirement(invalid_integrations)

        invalid_human = {
            "assistant_id": "assistant",
            "assistant_name": "Assistant",
            "power_id": "power",
            "power_summary": "Summary",
            "interrupt_id": "interrupt",
            "request": {},
        }
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._human_requirement(invalid_human)
        invalid_human["request"] = {"kind": "unknown"}
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation._human_requirement(invalid_human)

    def test_decode_rejects_type_contract_kind_empty_and_binding_drift(self) -> None:
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation.decode(object())

        body = {
            "schema": 2,
            "kind": "integrations",
            "requirements": [],
            "pending": self.raw_pending,
        }
        stored = continuation_store.StoredContinuation(
            "team_1",
            "integrations",
            "a" * 32,
            2_000,
            1,
            ("binding",),
            json.dumps(body).encode(),
        )
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation.decode(stored)

        body["schema"] = 1
        body["kind"] = "unknown"
        stored = continuation_store.StoredContinuation(
            "team_1",
            "unknown",
            "a" * 32,
            2_000,
            1,
            ("binding",),
            json.dumps(body).encode(),
        )
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation.decode(stored)

        body["kind"] = "integrations"
        stored = continuation_store.StoredContinuation(
            "team_1",
            "integrations",
            "a" * 32,
            2_000,
            1,
            ("binding",),
            json.dumps(body).encode(),
        )
        with self.assertRaises(continuation.ContinuationCodecError):
            continuation.decode(stored)


if __name__ == "__main__":
    unittest.main()
