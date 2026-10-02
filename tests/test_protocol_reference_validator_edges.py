"""Behavioral edge coverage for vendored protocol reference validators."""

from __future__ import annotations

import copy
import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from protocol.assistant.v1.validators import human_request as human
from protocol.assistant.v1.validators import input_file as files_module
from protocol.assistant.v1.validators import message_catalog as catalog_module
from protocol.http.v1 import websocket
from protocol.install.v1 import schema_validator as schema

ROOT = Path(__file__).resolve().parents[1]
ASSISTANT_PROTOCOL = ROOT / "protocol" / "assistant" / "v1"
SUMMARY = "Approve reviewed changes."


def _message(text: str, max_length: int = 80, params: tuple[tuple[str, str, int], ...] = ()) -> dict[str, object]:
    return {
        "id": catalog_module.message_id(text),
        "msgid": text,
        "max_length": max_length,
        "params": [{"name": name, "kind": kind, "max_length": bound} for name, kind, bound in params],
    }


MESSAGES = sorted(
    (
        _message(SUMMARY, 160),
        _message("Approve"),
        _message("Approve this action", 500),
        _message("Name"),
        _message("Region"),
        _message("A"),
        _message("B"),
        _message("Second", 160),
        _message("Example: {zone}", 120, (("zone", "domain", 100),)),
        _message("Changes: {count}. Record: {record}.", 80, (("count", "integer", 3), ("record", "identifier", 32))),
    ),
    key=lambda message: message["id"],
)
CATALOG = {message["id"]: message for message in MESSAGES}


def _ref(text: str, **params: object) -> dict[str, object]:
    return {"message": catalog_module.message_id(text), "params": params}


def _approval(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": "approval",
        "ordinal": 0,
        "title": _ref("Approve"),
        "description": _ref("Approve this action"),
    }
    value.update(changes)
    return value


def _text_request(**changes: object) -> dict[str, object]:
    value = {
        **_approval(kind="input:text"),
        "label": _ref("Name"),
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
        "label": _ref("Region"),
        "required": True,
        "options": [
            {"value": "a", "label": _ref("A"), "description": None},
            {"value": "b", "label": _ref("B"), "description": _ref("Second")},
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
        vectors = json.loads((ASSISTANT_PROTOCOL / "vectors/human-request.json").read_bytes())
        machine = json.loads((ASSISTANT_PROTOCOL / "machine-contract.schema.json").read_bytes())
        human.verify_vectors(vectors, machine["$defs"]["humanRequestCapability"]["enum"])
        self.assertIsNone(catalog_module.catalog_error(MESSAGES, SUMMARY))
        requests = (
            _approval(),
            _approval(kind="auth:password"),
            _approval(kind="auth:totp"),
            _approval(kind="auth:passkey"),
            _approval(title=_ref("Changes: {count}. Record: {record}.", count=12, record="rec_01:A.b-c")),
            _text_request(),
            _text_request(placeholder=_ref("Example: {zone}", zone="example.com")),
            _text_request(kind="input:textarea", max_length=16000),
            _text_request(kind="input:password", max_length=1024),
            _text_request(
                kind="input:password",
                max_length=1024,
                stored_input="whatsapp-token",
            ),
            _text_request(kind="input:phone", max_length=64),
            _choice_request(),
            _choice_request(kind="input:choice"),
            _choice_request(multiple=True),
        )
        self.assertTrue(all(human.request_error(request, CATALOG) is None for request in requests))

    def test_request_validation_rejects_base_length_and_choice_edges(self) -> None:
        options = _choice_request()["options"]
        cases = (
            (None, "request_shape"),
            ({"kind": "approval", "ordinal": 0}, "request_shape"),
            (_approval(ordinal=True), "request_shape"),
            (_approval(title="Approve"), "copy_reference"),
            (_approval(title=None), "copy_reference"),
            (_approval(title={"message": None, "params": {}}), "copy_reference"),
            (_approval(title={"message": catalog_module.message_id("Approve"), "params": []}), "copy_reference"),
            (_approval(title=_ref("Approve this action")), "copy_bound"),
            (_approval(extra=True), "request_shape"),
            (_approval(kind="unknown"), "request_kind"),
            (_text_request(required=1), "request_shape"),
            (_text_request(placeholder=_ref("Example: {zone}", zone="not a domain")), "copy_params"),
            (_text_request(label=None), "copy_reference"),
            (_text_request(min_length=True), "length_bounds"),
            (_text_request(min_length=2, max_length=1), "length_bounds"),
            (_text_request(stored_input="whatsapp-token"), "request_shape"),
            (
                _text_request(
                    kind="input:password",
                    max_length=1024,
                    stored_input="WhatsApp_Token",
                ),
                "stored_input",
            ),
            (_choice_request(options=[]), "options"),
            (
                _choice_request(
                    options=[
                        {"value": "a", "label": _ref("A"), "description": None},
                        {"value": "a", "label": _ref("B"), "description": None},
                    ]
                ),
                "options",
            ),
            (_choice_request(label="Region"), "copy_reference"),
            (_choice_request(options=[options[0], {**options[1], "description": "Second"}]), "copy_reference"),
            (_choice_request(options=[options[0], {**options[1], "label": None}]), "copy_reference"),
            (_choice_request(multiple=True, min_selections=True), "selection_bounds"),
        )
        for request, expected in cases:
            with self.subTest(expected=expected, request=request):
                self.assertEqual(human.request_error(request, CATALOG), expected)

    def test_reference_parameters_follow_their_declared_kind_and_length(self) -> None:
        changes = "Changes: {count}. Record: {record}."
        cases = (
            ({"count": 999, "record": "x" * 32}, None),
            ({"count": 0, "record": "A"}, None),
            ({"count": 1000, "record": "a"}, "copy_params"),
            ({"count": -1, "record": "a"}, "copy_params"),
            ({"count": True, "record": "a"}, "copy_params"),
            ({"count": "1", "record": "a"}, "copy_params"),
            ({"count": 1, "record": "x" * 33}, "copy_params"),
            ({"count": 1, "record": "-leading"}, "copy_params"),
            ({"count": 1, "record": "two words"}, "copy_params"),
            ({"count": 1, "record": 7}, "copy_params"),
            ({"count": 1}, "copy_params"),
            ({"count": 1, "record": "a", "extra": "a"}, "copy_params"),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(human.reference_error(_ref(changes, **params), CATALOG, 80), expected)
        example = "Example: {zone}"
        self.assertEqual(human.reference_error(_ref(changes, count=10**5000, record="a"), CATALOG, 80), "copy_params")
        for zone, expected in (
            ("example.com", None),
            ("a.b", None),
            ("xn--caf-dma.example", None),
            ("localhost", "copy_params"),
            ("Example.com", "copy_params"),
            ("example.com.", "copy_params"),
            ("-bad.example", "copy_params"),
            ("a" * 97 + ".com", "copy_params"),
        ):
            with self.subTest(zone=zone):
                self.assertEqual(human.reference_error(_ref(example, zone=zone), CATALOG, 120), expected)
        self.assertEqual(human.reference_error(_ref(example, zone="example.com"), CATALOG, 80), "copy_bound")
        self.assertEqual(human.reference_error(None, CATALOG, 80), "copy_reference")
        self.assertEqual(human.reference_error(_ref("Unknown"), CATALOG, 80), "copy_reference")

    def test_integer_parameters_compare_numerically_at_every_declared_length(self) -> None:
        for maximum in (1, catalog_module.PARAM_BOUNDS["integer"]):
            declaration = {"name": "count", "kind": "integer", "max_length": maximum}
            cases = (
                (0, True),
                (10**maximum - 1, True),
                (10**maximum, False),
                (-1, False),
                (True, False),
                (False, False),
                (1.0, False),
                (10**5000, False),
                (-(10**5000), False),
            )
            for value, expected in cases:
                with self.subTest(maximum=maximum, value=type(value).__name__ if abs(value) > 10**20 else value):
                    self.assertIs(human.param_value(declaration, value), expected)

    def test_dns_name_parameters_admit_only_exact_record_names_within_their_bound(self) -> None:
        declaration = {"name": "name", "kind": "dns_name", "max_length": 30}
        for value, expected in (
            ("_acme-challenge.example.com", True),
            ("_dmarc", True),
            ("a_b_.c", True),
            ("x" * 30, True),
            ("x" * 31, False),
            ("*.example.com", False),
            ("_dmarc.example.com.", False),
            ("_dmarc..example.com", False),
            ("-dmarc.example.com", False),
            ("dmarc-.example.com", False),
            ("_DMARC.example.com", False),
            ("exämple.com", False),
            ("dmarc example", False),
            ("", False),
            (5, False),
        ):
            with self.subTest(value=value):
                self.assertIs(human.param_value(declaration, value), expected)
        longest = {"name": "name", "kind": "dns_name", "max_length": catalog_module.PARAM_BOUNDS["dns_name"]}
        self.assertTrue(human.param_value(longest, ".".join(["a" * 63] * 3 + ["b" * 61])))
        self.assertFalse(human.param_value(longest, "a" * 64 + ".example.com"))

    def test_transcript_validation_covers_order_count_and_response_semantics(self) -> None:
        approval = _approval()
        text = _text_request()
        select = _choice_request()
        optional = _choice_request(required=False)
        choices = _choice_request(multiple=True)
        cases = (
            (None, [], "transcript_shape"),
            ([_approval()] * 9, [], "transcript_shape"),
            ([_approval(ordinal=1)], [], "ordinal_sequence"),
            ([_approval(extra=True)], [], "request_shape"),
            ([_approval(title=_ref("Unknown"))], [], "copy_reference"),
            ([_text_request(kind="input:password"), _approval(ordinal=1)], [], "secret_last"),
            (
                [_approval(), _approval(kind="auth:password", ordinal=1)],
                [],
                "authorization_once",
            ),
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
                self.assertEqual(human.transcript_error(requests, responses, CATALOG), expected)

    def test_fingerprint_binds_references_and_parameters(self) -> None:
        first = _approval(title=_ref("Changes: {count}. Record: {record}.", count=1, record="a"))
        second = _approval(title=_ref("Changes: {count}. Record: {record}.", count=2, record="a"))
        self.assertNotEqual(human.fingerprint(first), human.fingerprint(second))
        self.assertEqual(human.fingerprint(first), human.fingerprint(copy.deepcopy(first)))

    def test_vector_sections_and_helpers_are_closed(self) -> None:
        document = json.loads((ASSISTANT_PROTOCOL / "vectors/human-request.json").read_bytes())
        capabilities = copy.deepcopy(document["capabilities"])
        for value in (
            [],
            {**document, "extra": True},
            {**document, "version": 2},
            {**document, "limits": {}},
            {**document, "catalog": []},
            {**document, "catalog": {"messages": []}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                human.verify_vectors(value, capabilities)
        with (
            mock.patch.object(human, "_verify_fingerprints", side_effect=ValueError("fingerprint")),
            self.assertRaisesRegex(ValueError, "fingerprint"),
        ):
            human.verify_vectors(document, capabilities)

        request = _approval()
        fingerprints = {
            "algorithm": "sha256",
            "serialization": "utf8-json-sort-keys-compact-no-ascii-escaping",
            "cases": [
                {"name": "one", "request": request, "sha256": human.fingerprint(request)},
                {
                    "name": "two",
                    "request": _approval(title=_ref("Name")),
                    "sha256": human.fingerprint(_approval(title=_ref("Name"))),
                },
            ],
        }
        invalid_sections = (
            None,
            {**fingerprints, "algorithm": "bad"},
            {**fingerprints, "cases": []},
            {**fingerprints, "cases": [None, None]},
            {**fingerprints, "cases": [{**fingerprints["cases"][0], "sha256": "0" * 64}, fingerprints["cases"][1]]},
        )
        for section in invalid_sections:
            with self.subTest(section=section), self.assertRaises(ValueError):
                human._verify_fingerprints(section)

        valid_cases = [
            {"name": "valid", "valid": True, "request": request},
            {"name": "invalid", "valid": False, "request": _approval(extra=True), "error": "request_shape"},
        ]
        human._verify_cases(valid_cases, "request", CATALOG)
        invalid_cases = (
            None,
            [],
            [{}],
            [valid_cases[0], {**valid_cases[0]}],
            [{**valid_cases[0], "valid": False, "error": "wrong"}, valid_cases[1]],
            [valid_cases[0]],
        )
        for cases in invalid_cases:
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                human._verify_cases(cases, "request", CATALOG)

        self.assertEqual(human.request_error(_choice_request(required=1), CATALOG), "request_shape")
        self.assertFalse(human._option(None))
        self.assertFalse(human._option({"value": "a"}))
        self.assertFalse(human._option({"value": " a", "label": _ref("A"), "description": None}))
        self.assertFalse(human._text("bad\n", 10))
        self.assertFalse(human._identifier(None))


class MessageCatalogValidatorEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vectors = json.loads((ASSISTANT_PROTOCOL / "vectors/catalog.json").read_bytes())
        self.messages = self.vectors["catalog"]["messages"]
        self.pack = self.vectors["pack"]["value"]

    def test_current_vectors_verify(self) -> None:
        catalog_module.verify_vectors(copy.deepcopy(self.vectors), human.reference_error)
        raw = catalog_module.canonical_json(self.pack)
        self.assertEqual(catalog_module.pack_digest(raw), self.vectors["pack"]["digest"])
        self.assertEqual(catalog_module.catalog_digest(self.messages), self.vectors["catalog"]["digest"])

    def test_placeholders_admit_only_named_fields(self) -> None:
        self.assertEqual(catalog_module.placeholders("No fields"), [])
        self.assertEqual(catalog_module.placeholders("{a} and {b_2}"), ["a", "b_2"])
        for template in ("{", "}", "} {a}", "{a", "{a.b}", "{a[0]}", "{a!r}", "{a:>2}", "{}", "{0}", "{{a}}", "{A}"):
            with self.subTest(template=template):
                self.assertIsNone(catalog_module.placeholders(template))

    def test_combining_mark_after_placeholder_is_refused_because_rendering_would_leave_nfc(self) -> None:
        reference = {"message": "unused", "params": {"x": "e"}}
        self.assertFalse(catalog_module.public_text(catalog_module.render(reference, "{x}́"), 80))
        summary = _message(SUMMARY, 160)
        for template, expected in (
            ("Zone {x}́", "message_placeholders"),
            ("Zone {x}ः", "message_placeholders"),
            ("Zone {x}⃝", "message_placeholders"),
            ("Zone {x}́ more", "message_placeholders"),
            ("Zone {x}é", None),
            ("Zone {x} ́", None),
            ("Zone x́ {x}", None),
            ("Zone {x}", None),
        ):
            with self.subTest(template=ascii(template)):
                messages = sorted((summary, _message(template, 80, (("x", "identifier", 8),))), key=lambda m: m["id"])
                self.assertEqual(catalog_module.catalog_error(messages, SUMMARY), expected)
        translated = copy.deepcopy(self.pack)
        message = next(message for message in self.messages if message["params"])
        field = "{" + message["params"][0]["name"] + "}"
        translated["locales"]["de"][message["id"]] = translated["locales"]["de"][message["id"]].replace(
            field, field + "́"
        )
        raw = catalog_module.canonical_json(translated)
        self.assertEqual(catalog_module.pack_error(raw, self.messages), "translation_placeholders")

    def test_value_count_is_iterative_and_stops_above_its_limit(self) -> None:
        exceed = catalog_module._json_values_exceed
        self.assertFalse(exceed([1, {"a": [2]}], 5))
        self.assertTrue(exceed([1, {"a": [2]}], 4))
        self.assertFalse(exceed(catalog_module.nested_catalog(SUMMARY, 4090), catalog_module.MAX_CATALOG_VALUES))
        self.assertTrue(exceed(catalog_module.nested_catalog(SUMMARY, 4091), catalog_module.MAX_CATALOG_VALUES))
        self.assertTrue(exceed(catalog_module.nested_catalog(SUMMARY, 100_000), catalog_module.MAX_CATALOG_VALUES))
        self.assertTrue(exceed([0] * 1_000_000, catalog_module.MAX_CATALOG_VALUES))

    def test_deep_nesting_is_refused_instead_of_raising(self) -> None:
        self.assertEqual(
            catalog_module.catalog_error(catalog_module.nested_catalog(SUMMARY, 1100), SUMMARY), "message_shape"
        )
        self.assertEqual(
            catalog_module.catalog_error(catalog_module.nested_catalog(SUMMARY, 100_000), SUMMARY), "catalog_bounds"
        )
        nested = catalog_module.nested_catalog(SUMMARY, 4000)
        outcomes: list[object] = []

        def small_stack() -> None:
            # Precondition: this stack cannot encode admissibly counted nesting, so the encoder path is exercised.
            with self.assertRaises(RecursionError):
                catalog_module.canonical_json(nested)
            outcomes.append(catalog_module.catalog_error(nested, SUMMARY))

        previous = threading.stack_size(256 * 1024)
        try:
            worker = threading.Thread(target=small_stack)
            worker.start()
            worker.join()
        finally:
            threading.stack_size(previous)
        self.assertEqual(outcomes, ["message_shape"])
        depth = 1_000_000
        self.assertEqual(catalog_module.pack_error(b"[" * depth + b"]" * depth, self.messages), "pack_encoding")

    def test_render_vectors_prove_only_admitted_references_and_field_bounds(self) -> None:
        document = self.vectors
        base = next(case for case in document["render_cases"] if case["reference"]["params"].get("count") == 3)
        message = next(message for message in self.messages if message["id"] == base["reference"]["message"])
        template = message["msgid"] if base["locale"] == "en" else self.pack["locales"][base["locale"]][message["id"]]

        def variant(bound: object = None, **params: object) -> dict[str, object]:
            reference = {**base["reference"], "params": {**base["reference"]["params"], **params}}
            rendered = catalog_module.render(reference, template)
            return {
                **base,
                "reference": reference,
                "bound": base["bound"] if bound is None else bound,
                "rendered": rendered,
            }

        catalog_module.verify_vectors({**document, "render_cases": [variant(count=9999)]}, human.reference_error)
        mutations = (
            variant(count=-1),
            variant(count=10_000),
            variant(count=True),
            variant(zone="Example.com"),
            variant(zone="localhost"),
            variant(bound=79),
            variant(bound=True),
            variant(bound="500"),
            variant(bound=80),
            {**variant(), "reference": {**base["reference"], "extra": 1}},
            {**variant(), "reference": {"message": "0" * 64, "params": {}}},
        )
        for case in mutations:
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, "render_cases"):
                catalog_module.verify_vectors({**document, "render_cases": [case]}, human.reference_error)

    def test_catalog_errors_cover_shape_bounds_and_summary(self) -> None:
        summary = _message(SUMMARY, 160)
        cases = (
            (None, "catalog_shape"),
            ([summary, float("nan")], "catalog_shape"),
            ([summary, {1, 2}], "catalog_shape"),
            ([summary, None], "message_shape"),
            ([summary, {**_message("Zone"), "max_length": True}], "message_shape"),
            ([summary, {**_message("Zone"), "max_length": 81}], "message_shape"),
            ([summary, {**_message("Zone"), "id": None}], "message_id"),
            ([summary, {**_message("Zone"), "params": None}], "message_params"),
            ([summary, {**_message("Zone"), "params": [None]}], "message_params"),
            (
                [summary, {**_message("Zone"), "params": [{"name": 1, "kind": "integer", "max_length": 1}]}],
                "message_params",
            ),
            (
                [summary, {**_message("Zone {a}"), "params": [{"name": "a", "kind": "integer", "max_length": True}]}],
                "message_params",
            ),
            (
                [summary, {**_message("Zone {a}"), "params": [{"name": "a", "kind": [], "max_length": 1}]}],
                "message_params",
            ),
            (
                [summary, {**_message("Zone {a}"), "params": [{"name": "a", "kind": {}, "max_length": 1}]}],
                "message_params",
            ),
            (
                [summary, {**_message("Zone {a}"), "params": [{"name": "a", "kind": "integer", "max_length": 0}]}],
                "message_params",
            ),
            (
                [
                    summary,
                    _message(
                        "{a} {b} {c} {d} {e} {f} {g} {h} {i}", 500, tuple((name, "integer", 1) for name in "abcdefghi")
                    ),
                ],
                "message_params",
            ),
            ([summary, _message("{a} {a}", 80, (("a", "integer", 1),))], "message_placeholders"),
            ([summary, _message("{a}", 80, (("a", "integer", 1), ("b", "integer", 1)))], "message_placeholders"),
            ([_message(SUMMARY, 80)], None),
            ([_message(SUMMARY, 500)], "catalog_summary"),
            ([_message("Other", 160)], "catalog_summary"),
        )
        for messages, expected in cases:
            with self.subTest(expected=expected, messages=messages):
                self.assertEqual(catalog_module.catalog_error(messages, SUMMARY), expected)

    def test_pack_errors_cover_bytes_encoding_shape_and_translations(self) -> None:
        raw = catalog_module.canonical_json(self.pack)
        self.assertIsNone(catalog_module.pack_error(raw, self.messages))
        self.assertEqual(catalog_module.pack_error("text", self.messages), "pack_bytes")
        self.assertEqual(
            catalog_module.pack_error(b" " * (catalog_module.MAX_PACK_BYTES + 1), self.messages), "pack_bytes"
        )
        for encoded in (b"\xff", b"{", b"NaN", b'{"a":1,"a":1}'):
            with self.subTest(encoded=encoded):
                self.assertEqual(catalog_module.pack_error(encoded, self.messages), "pack_encoding")
        shapes = (
            [],
            {**self.pack, "catalog": 1},
            {**self.pack, "locales": []},
            {**self.pack, "locales": {**self.pack["locales"], "ar": []}},
        )
        for value in shapes:
            with self.subTest(value=value):
                encoded = catalog_module.canonical_json(value)
                self.assertEqual(catalog_module.pack_error(encoded, self.messages), "pack_shape")

    def test_vector_document_and_sections_are_closed(self) -> None:
        document = self.vectors
        for value in ([], {**document, "extra": True}, {**document, "version": 2}, {**document, "limits": {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                catalog_module.verify_vectors(value, human.reference_error)
        catalog = document["catalog"]
        for section in (None, {**catalog, "summary": "Missing"}, {**catalog, "digest": "sha256:" + "0" * 64}):
            with self.subTest(section=section), self.assertRaises(ValueError):
                catalog_module.verify_vectors({**document, "catalog": section}, human.reference_error)
        pack = document["pack"]
        for section in (None, {**pack, "digest": "sha256:" + "0" * 64}):
            with self.subTest(section=section), self.assertRaises(ValueError):
                catalog_module.verify_vectors({**document, "pack": section}, human.reference_error)
        render = document["render_cases"][0]
        for cases in (
            None,
            [],
            [None],
            [{**render, "reference": {**render["reference"], "params": {"extra": 1}}}],
            [{**render, "rendered": "Different"}],
        ):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                catalog_module.verify_vectors({**document, "render_cases": cases}, human.reference_error)
        valid = document["pack_cases"][0]
        invalid = document["pack_cases"][1]
        for cases in (
            None,
            [],
            [None],
            [{**valid, "text": "{}"}],
            [valid, {**valid}],
            [{**valid, "name": ""}],
            [{**valid, "valid": False, "error": "pack_shape"}, invalid],
            [valid],
        ):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                catalog_module.verify_vectors({**document, "pack_cases": cases}, human.reference_error)


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
        self.assertEqual(websocket.public_text("français\u00a0?", 10), "français\u00a0?")
        for value in ("line\u2028break", "paragraph\u2029break", "hidden\u200dinstruction"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                websocket.public_text(value, 40)
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
            ({"type": "array", "contains": {"type": "string"}, "minContains": 2}, ["one", 2]),
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


class AssistantFileInputValidatorTests(unittest.TestCase):
    def test_reference_validator_reproduces_every_published_verdict(self) -> None:
        for filename, fields, error in (
            ("input-file.json", ("actions",), files_module.input_files_error),
            ("file-invocation.json", ("action", "invocation"), files_module.invocation_files_error),
        ):
            for case in json.loads((ASSISTANT_PROTOCOL / "vectors" / filename).read_bytes())["cases"]:
                with self.subTest(vector=case["name"]):
                    self.assertEqual(error(*(case[field] for field in fields)) is None, case["valid"])

    def test_declarations_and_values_outside_json_are_refused(self) -> None:
        action = {
            "input_files": ["file"],
            "input_schema": {"properties": {"file": {**files_module.FILE_ID_SCHEMA, "x": float("nan")}}},
            "human_requests": ["approval"],
        }
        action["input_schema"]["required"] = ["file"]
        self.assertEqual(files_module.declaration_error(action), "input_file_schema")
        self.assertFalse(files_module.valid_name("bad\udc00name"))

    def test_shapes_outside_the_vectors_are_refused_with_stable_reasons(self) -> None:
        action = {"input_files": [], "human_requests": ["input:choices"]}
        self.assertEqual(files_module.input_files_error({"input_files": []}), "actions_invalid")
        self.assertEqual(
            files_module.declaration_error({"input_files": ["file"], "input_schema": []}), "input_file_unknown"
        )
        self.assertEqual(files_module.invocation_files_error([], {"files": {}}), "invocation_invalid")
        self.assertEqual(files_module.responses_error(action, {}), "responses_invalid")
        response = {"ordinal": 0, "fingerprint": "0" * 64, "kind": "input:choices"}
        for value, error in ((["a", "b"], None), (["a", "a"], "responses_invalid"), ("a", "responses_invalid")):
            with self.subTest(value=value):
                self.assertEqual(files_module.responses_error(action, [{**response, "value": value}]), error)

    def test_content_decoding_is_canonical_and_bounded(self) -> None:
        self.assertEqual(files_module.decode_content("YWI="), b"ab")
        for text in ("YWJ=", "YWI", "YW I=", "-_8=", 7, "A" * (files_module.MAX_BASE64_CHARACTERS + 4)):
            with self.subTest(text=text):
                self.assertIsNone(files_module.decode_content(text))
        self.assertEqual(files_module.MAX_BASE64_CHARACTERS, 11_184_812)

    def test_only_delivered_content_admits_the_larger_invocation_bound(self) -> None:
        record = {"name": "a.txt", "media_type": "text/plain", "size": 1, "sha256": "0" * 64}
        delivered = {"files": {"0" * 32: {**record, "content": {"type": "delivered", "base64": "YQ=="}}}}
        withheld = {"files": {"0" * 32: {**record, "content": {"type": "withheld"}}}}
        self.assertTrue(files_module.delivers_content(delivered))
        self.assertFalse(files_module.delivers_content(withheld))
        self.assertFalse(files_module.delivers_content({"files": {}}))
        self.assertFalse(files_module.delivers_content(None))
        self.assertLess(
            files_module.MAX_BASE64_CHARACTERS + files_module.MAX_INVOCATION_BYTES,
            files_module.MAX_FILE_INVOCATION_BYTES,
        )

    def test_files_shape_matches_every_schema_level_invocation_vector(self) -> None:
        vectors = json.loads((ASSISTANT_PROTOCOL / "vectors/invocation.json").read_bytes())
        for case in vectors["cases"]:
            files = case["invocation"].get("files")
            if files is None or not case["valid"]:
                continue
            with self.subTest(case=case["name"]):
                self.assertIsNone(files_module.files_shape_error(files))
        record = {"name": "a.txt", "media_type": "text/plain", "size": 1, "sha256": "0" * 64}
        for files in (
            None,
            {
                "0" * 32: {**record, "content": {"type": "withheld"}},
                "1" * 32: {**record, "content": {"type": "withheld"}},
            },
            {"0" * 31: {**record, "content": {"type": "withheld"}}},
            {"0" * 32: {**record, "content": {"type": "delivered"}}},
            {"0" * 32: {**record, "content": {"type": "withheld", "base64": "YQ=="}}},
            {"0" * 32: {**record, "size": 0, "content": {"type": "withheld"}}},
        ):
            with self.subTest(files=files):
                self.assertIsNotNone(files_module.files_shape_error(files))

    def test_names_are_literal_bounded_data(self) -> None:
        self.assertTrue(files_module.valid_name("Relatório de março.pdf"))
        for name in ("", " a", "a ", ".", "..", "a/b", "a\\b", "a\x00", "a\x7f", "\ud800", "é" * 128):
            with self.subTest(name=name):
                self.assertFalse(files_module.valid_name(name))
        self.assertTrue(files_module.valid_name("a" * 255))


if __name__ == "__main__":
    unittest.main()
