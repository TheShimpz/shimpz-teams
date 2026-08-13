"""Behavioral edge coverage for vendored protocol reference validators."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from protocol.assistant.v1 import human_request_validator as human
from protocol.http.v1 import websocket
from protocol.install.v1 import schema_validator as schema

ROOT = Path(__file__).resolve().parents[1]


def _approval(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": "approval",
        "ordinal": 0,
        "title": "Approve",
        "description": "Approve this action",
    }
    value.update(changes)
    return value


def _text_request(**changes: object) -> dict[str, object]:
    value = {
        **_approval(kind="input:text"),
        "label": "Name",
        "required": True,
        "placeholder": None,
        "min_length": 1,
        "max_length": 20,
    }
    value.update(changes)
    return value


def _choice_request(*, multiple: bool = False, **changes: object) -> dict[str, object]:
    value = {
        **_approval(kind="input:choices" if multiple else "input:select"),
        "label": "Region",
        "required": True,
        "options": [
            {"value": "a", "label": "A", "description": None},
            {"value": "b", "label": "B", "description": "Second"},
        ],
    }
    if multiple:
        value.update({"min_selections": 1, "max_selections": 2})
    value.update(changes)
    return value


def _response(request: dict[str, object], value: object, **changes: object) -> dict[str, object]:
    response = {
        "kind": request["kind"],
        "ordinal": request["ordinal"],
        "fingerprint": human.fingerprint(request),
        "value": value,
    }
    response.update(changes)
    return response


class HumanRequestValidatorEdgeTests(unittest.TestCase):
    def test_current_vectors_and_every_request_family_are_accepted(self) -> None:
        protocol = ROOT / "protocol/assistant/v1"
        vectors = json.loads((protocol / "human-request-vectors.json").read_bytes())
        machine = json.loads((protocol / "machine-contract.schema.json").read_bytes())
        human.verify_vectors(vectors, machine["$defs"]["humanRequestCapability"]["enum"])

        requests = (
            _approval(),
            _approval(kind="auth:password"),
            _text_request(),
            _text_request(kind="input:textarea", max_length=16000),
            _text_request(kind="input:password", max_length=1024),
            _text_request(kind="input:phone", max_length=64),
            _choice_request(),
            _choice_request(kind="input:choice"),
            _choice_request(multiple=True),
        )
        self.assertTrue(all(human.request_error(request) is None for request in requests))

    def test_request_validation_rejects_base_length_and_choice_edges(self) -> None:
        cases = (
            (None, "request_shape"),
            (_approval(ordinal=True), "request_shape"),
            (_approval(title=" bad"), "public_text"),
            (_approval(extra=True), "request_shape"),
            (_approval(kind="unknown"), "request_kind"),
            (_text_request(required=1), "request_shape"),
            (_text_request(placeholder=" bad"), "public_text"),
            (_text_request(min_length=True), "length_bounds"),
            (_text_request(min_length=2, max_length=1), "length_bounds"),
            (_choice_request(options=[]), "options"),
            (
                _choice_request(
                    options=[
                        {"value": "a", "label": "A", "description": None},
                        {"value": "a", "label": "Again", "description": None},
                    ]
                ),
                "options",
            ),
            (_choice_request(multiple=True, min_selections=True), "selection_bounds"),
        )
        for request, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(human.request_error(request), expected)

    def test_transcript_validation_covers_order_count_and_response_semantics(self) -> None:
        approval = _approval()
        text = _text_request()
        select = _choice_request()
        optional = _choice_request(required=False)
        choices = _choice_request(multiple=True)
        cases = (
            (None, [], "transcript_shape"),
            ([_approval(ordinal=1)], [], "ordinal_sequence"),
            ([_approval(extra=True)], [], "request_shape"),
            ([_text_request(kind="input:password"), _approval(ordinal=1)], [], "secret_last"),
            ([approval], [], "response_count"),
            ([approval], [{}], "response_shape"),
            ([approval], [_response(approval, True, kind="auth:password")], "response_match"),
            ([approval], [_response(approval, True, fingerprint="bad")], "response_match"),
            ([approval], [_response(approval, False)], "response_value"),
            ([select], [_response(select, "a")], None),
            ([optional], [_response(optional, "")], None),
            ([select], [_response(select, "missing")], "response_value"),
            ([choices], [_response(choices, ["a", "a"])], "response_value"),
            ([choices], [_response(choices, ["missing"])], "response_value"),
            ([text], [_response(text, "valid")], None),
            ([text], [_response(text, 1)], "response_value"),
            ([text], [_response(text, "")], "response_value"),
            ([text], [_response(text, "x" * 21)], "response_value"),
        )
        for requests, responses, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(human.transcript_error(requests, responses), expected)

    def test_vector_envelope_checks_are_closed(self) -> None:
        protocol = ROOT / "protocol/assistant/v1"
        document = json.loads((protocol / "human-request-vectors.json").read_bytes())
        capabilities = copy.deepcopy(document["capabilities"])
        mutations = (
            None,
            {**document, "extra": True},
            {**document, "version": 2},
            {**document, "limits": {}},
        )
        for mutated in mutations:
            if mutated is None:
                value: object = []
            else:
                value = mutated
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                human.verify_vectors(value, capabilities)

        with (
            mock.patch.object(human, "_verify_fingerprints", side_effect=ValueError("fingerprint")),
            self.assertRaisesRegex(ValueError, "fingerprint"),
        ):
            human.verify_vectors(document, capabilities)

    def test_fingerprint_and_case_section_checks_reject_drift(self) -> None:
        request = _approval()
        digest = human.fingerprint(request)
        valid_fingerprints = {
            "algorithm": "sha256",
            "serialization": "utf8-json-sort-keys-compact-no-ascii-escaping",
            "cases": [
                {"name": "one", "request": request, "sha256": digest},
                {
                    "name": "two",
                    "request": _approval(title="Other"),
                    "sha256": human.fingerprint(_approval(title="Other")),
                },
            ],
        }
        invalid_sections = (
            None,
            {**valid_fingerprints, "algorithm": "bad"},
            {**valid_fingerprints, "cases": []},
            {**valid_fingerprints, "cases": [None, None]},
            {**valid_fingerprints, "cases": [{}]},
            {
                **valid_fingerprints,
                "cases": [{**valid_fingerprints["cases"][0], "sha256": "0" * 64}, valid_fingerprints["cases"][1]],
            },
        )
        for section in invalid_sections:
            with self.subTest(section=section), self.assertRaises(ValueError):
                human._verify_fingerprints(section)

        valid_cases = [
            {"name": "valid", "valid": True, "request": request},
            {"name": "invalid", "valid": False, "request": _approval(extra=True), "error": "request_shape"},
        ]
        human._verify_cases(valid_cases, "request")
        for cases in (
            None,
            [],
            [{}],
            [valid_cases[0], {**valid_cases[0]}],
            [{**valid_cases[0], "valid": False, "error": "wrong"}, valid_cases[1]],
            [valid_cases[0]],
        ):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                human._verify_cases(cases, "request")

    def test_private_text_option_and_response_helpers_reject_ambiguous_values(self) -> None:
        self.assertFalse(human._input_base_valid({"required": True, "label": " bad"}))
        self.assertEqual(human.request_error(_choice_request(required=1)), "request_shape")
        self.assertFalse(human._option(None))
        self.assertFalse(human._option({"value": "a"}))
        self.assertFalse(human._option({"value": "a", "label": "A", "description": " bad"}))
        self.assertFalse(human._text("bad\n", 10))


class WebSocketReferenceEdgeTests(unittest.TestCase):
    def test_origins_json_objects_and_public_text_are_canonical(self) -> None:
        self.assertEqual(websocket.canonical_origin("HTTPS://Example.COM:443"), "https://example.com:443")
        for value in (None, "null", "http://user@example.com", "http://example.com/path", "http://[bad"):
            self.assertIsNone(websocket.canonical_origin(value))
        self.assertEqual(websocket.unique_json_object([("a", 1)]), {"a": 1})
        with self.assertRaises(ValueError):
            websocket.unique_json_object([("a", 1), ("a", 2)])
        with self.assertRaises(ValueError):
            websocket._reject_json_constant("NaN")
        self.assertEqual(websocket.public_text("Ready", 10), "Ready")
        for value in (None, "", " bad", "bad\n", "x" * 11):
            with self.assertRaises(ValueError):
                websocket.public_text(value, 10)

    def test_frame_decoder_maps_transport_and_json_failures(self) -> None:
        class Unencodable(str):
            def encode(self, *_args, **_kwargs):
                raise UnicodeError

        cases = (
            ({"type": "other"}, 400),
            ({"type": "websocket.receive", "bytes": b"x"}, 415),
            ({"type": "websocket.receive", "text": "x" * 5}, 413),
            ({"type": "websocket.receive", "text": "{"}, 400),
            ({"type": "websocket.receive", "text": "[]"}, 400),
        )
        for message, status in cases:
            with self.subTest(status=status), self.assertRaises(websocket.FrameError) as caught:
                websocket.decode_bounded_json_frame(message, 4)
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(
            websocket.decode_bounded_json_frame({"type": "websocket.receive", "text": '{"ok":true}'}, 32),
            {"ok": True},
        )
        with self.assertRaises(websocket.FrameError):
            websocket.decode_bounded_json_frame(
                {"type": "websocket.receive", "text": Unencodable("value")},
                32,
            )

    def test_error_challenge_and_human_response_helpers_are_closed(self) -> None:
        self.assertEqual(websocket.safe_status(404), 404)
        self.assertEqual(websocket.safe_status(True), 502)
        self.assertEqual(
            websocket.error_terminal(400, "bad\ndetail", fallback_detail="failed", max_detail_chars=20),
            {"type": "error", "status": 400, "detail": "failed"},
        )
        challenge_id = "a" * 32
        self.assertTrue(websocket.valid_challenge_id(challenge_id))
        self.assertFalse(websocket.valid_challenge_id(None))
        self.assertIsNone(websocket.challenge_identity(None, "team"))
        self.assertIsNone(
            websocket.challenge_identity(
                {"team_id": "other", "challenge_id": challenge_id, "turn_id": challenge_id},
                "team",
            )
        )
        self.assertEqual(
            websocket.challenge_identity(
                {"team_id": "team", "challenge_id": challenge_id, "turn_id": challenge_id},
                "team",
            ),
            (challenge_id, challenge_id),
        )

        submitted = {"type": "human-response", "challenge_id": challenge_id, "decision": "submit", "value": True}
        denied = {"type": "human-response", "challenge_id": challenge_id, "decision": "deny"}
        self.assertEqual(websocket.canonical_human_response(submitted), submitted)
        self.assertEqual(websocket.canonical_human_response(denied), denied)
        for value in (
            None,
            {**submitted, "decision": "bad"},
            {**submitted, "extra": True},
            {**submitted, "value": object()},
        ):
            with self.subTest(value=value), self.assertRaises(websocket.FrameError):
                websocket.canonical_human_response(value)
        self.assertTrue(websocket._human_value("x"))
        self.assertTrue(websocket._human_value(["a", "b"]))
        self.assertFalse(websocket._human_value(["a", "a"]))


class SchemaReferenceEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identifier = "https://schemas.example/root.json"
        self.documents = {self.identifier: {"$id": self.identifier, "$defs": {"name": {"type": "string"}}}}

    def test_schema_shape_and_reference_walker_are_closed(self) -> None:
        schema.check_schema(True, self.documents, self.identifier)
        valid = {
            "$ref": "#/$defs/name",
            "allOf": [{"type": "string"}],
            "oneOf": [{"const": "a"}],
            "prefixItems": [{"type": "string"}],
            "$defs": {"child": {"type": "string"}},
            "properties": {"name": {"type": "string"}},
            "additionalProperties": {"type": "string"},
            "items": {"type": "string"},
            "not": {"const": "bad"},
        }
        schema.check_schema(valid, self.documents, self.identifier)
        for invalid in (None, {"unknown": True}, {"allOf": {}}, {"properties": []}):
            with self.subTest(invalid=invalid), self.assertRaises(schema.SchemaViolationError):
                schema.check_schema(invalid, self.documents, self.identifier)

    def test_resolver_handles_documents_fragments_and_escaped_keys(self) -> None:
        document = {"$defs": {"a/b": {"~key": {"type": "string"}}}}
        documents = {self.identifier: document}
        self.assertEqual(
            schema._resolve("#/$defs/a~1b/~0key", documents, self.identifier),
            ({"type": "string"}, self.identifier),
        )
        self.assertEqual(schema._resolve(self.identifier, documents, self.identifier), (document, self.identifier))
        for reference in ("other.json", "#name", "#/$defs/missing"):
            with self.subTest(reference=reference), self.assertRaises(schema.SchemaViolationError):
                schema._resolve(reference, documents, self.identifier)

    def test_validator_covers_literals_types_objects_arrays_and_scalars(self) -> None:
        schema.validate(True, None, {}, self.identifier)
        schema.validate({}, None, {}, self.identifier)
        schema.validate(
            {"type": "string", "minLength": 1, "maxLength": 3, "pattern": "^[a-z]+$"}, "abc", {}, self.identifier
        )
        schema.validate(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
                "additionalProperties": {"type": "integer"},
            },
            {"name": "a", "count": 1},
            {},
            self.identifier,
        )
        schema.validate(
            {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"type": "string"}},
            ["a", "b"],
            {},
            self.identifier,
        )
        schema.validate({"type": "integer", "minimum": 1}, 1, {}, self.identifier)

        invalid = (
            (False, None),
            (None, None),
            ({"const": 1}, 2),
            ({"enum": [1]}, 2),
            ({"type": "string"}, 1),
            ({"type": "number"}, 1),
            ({"type": "object", "required": ["x"]}, {}),
            ({"type": "object", "properties": [], "additionalProperties": False}, {}),
            ({"type": "object", "properties": {}, "additionalProperties": False}, {"x": 1}),
            ({"type": "array", "minItems": 2}, []),
            ({"type": "array", "maxItems": 1}, [1, 2]),
            ({"type": "array", "uniqueItems": True}, [1, 1]),
            ({"type": "string", "minLength": 2}, "a"),
            ({"type": "string", "maxLength": 1}, "ab"),
            ({"type": "string", "pattern": "^a$"}, "b"),
            ({"type": "integer", "minimum": 2}, 1),
        )
        for contract, value in invalid:
            with self.subTest(contract=contract), self.assertRaises(schema.SchemaViolationError):
                schema.validate(contract, value, {}, self.identifier)

        schema._validate_object({}, None, {}, self.identifier, "$")
        schema._validate_array({}, None, {}, self.identifier, "$")
        schema._validate_string({}, None, "$")
        schema.validate({"type": "object"}, {"ignored": 1}, {}, self.identifier)

    def test_combinators_references_prefix_items_and_json_identity(self) -> None:
        documents = {self.identifier: {"$defs": {"text": {"type": "string"}}}}
        schema.validate({"$ref": "#/$defs/text"}, "ok", documents, self.identifier)
        schema.validate(
            {"allOf": [{"type": "string"}], "oneOf": [{"const": "ok"}, {"const": "no"}]}, "ok", {}, self.identifier
        )
        schema.validate(
            {"type": "array", "prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
            ["first", 2],
            {},
            self.identifier,
        )
        schema.validate({"allOf": {}}, "ok", {}, self.identifier)
        for contract in (
            {"oneOf": [{"type": "string"}, {"const": "ok"}]},
            {"not": {"type": "string"}},
        ):
            with self.assertRaises(schema.SchemaViolationError):
                schema.validate(contract, "ok", {}, self.identifier)
        self.assertTrue(schema._accepts({"type": "string"}, "ok", {}, self.identifier, "$"))
        self.assertFalse(schema._accepts({"type": "integer"}, "ok", {}, self.identifier, "$"))
        self.assertTrue(schema._json_equal({"b": 1, "a": 2}, {"a": 2, "b": 1}))


if __name__ == "__main__":
    unittest.main()
