"""Machine-contract Action schema admission: closed objects, self-contained references, and bounded validation."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from test_assistant_manifest import FIXTURE_SUMMARY, _reviewed_catalog

from assistant import action_schema
from assistant import manifest as assistant_manifest

REFERENCE_CONTRACT = Path(__file__).resolve().parent / "fixtures" / "reference-assistant" / "shimpz.contract.json"


class AssistantActionSchemaTests(unittest.TestCase):
    def test_machine_contract_loader_rejects_open_top_level_and_nested_schemas(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        open_contracts = []
        for schema_name in ("input_schema", "output_schema"):
            contract = json.loads(json.dumps(reviewed.machine_contract))
            contract["actions"][0][schema_name].pop("additionalProperties")
            open_contracts.append((schema_name, contract))
        nested = json.loads(json.dumps(reviewed.machine_contract))
        nested["actions"][0]["output_schema"]["properties"]["pagination"].pop("additionalProperties")
        open_contracts.append(("nested output schema", nested))

        for label, contract in open_contracts:
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(
                    assistant_manifest.ManifestError,
                    "must close every object",
                ),
            ):
                assistant_manifest.parse_machine_contract(
                    json.dumps(contract).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
                )

    def test_machine_schema_closes_typeless_objects_and_rejects_boolean_subschemas(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]

        typeless = json.loads(json.dumps(reviewed.machine_contract))
        typeless["actions"][0]["input_schema"]["properties"]["page"] = {"properties": {"value": {"type": "string"}}}
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "must close every object"):
            assistant_manifest.parse_machine_contract(
                json.dumps(typeless).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
            )

        boolean = json.loads(json.dumps(reviewed.machine_contract))
        boolean["actions"][0]["input_schema"]["properties"]["page"] = True
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.parse_machine_contract(
                json.dumps(boolean).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
            )

        literals = json.loads(json.dumps(reviewed.machine_contract))
        literals["actions"][0]["input_schema"]["properties"].update(
            {
                "flag": {"type": "boolean", "enum": [True, False]},
                "choice": {"enum": [True, False]},
                "fixed": {"const": True},
            }
        )
        parsed = assistant_manifest.parse_machine_contract(
            json.dumps(literals).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
        )

        self.assertEqual(
            parsed["actions"][0]["input_schema"]["properties"]["flag"],
            {"type": "boolean", "enum": [True, False]},
        )

    def test_machine_schema_admits_only_root_or_named_definition_references(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        refused = (
            "https://example.test/schema.json",
            "file:///etc/passwd",
            "other.json#/a",
            "#/default",
            "#/properties/page",
            "#/$defs/page/default",
            "#/$defs/page%2Fdefault",
            "#/%24defs/page",
            "#/$defs/",
            "#page",
        )
        for keyword, references in (("$ref", refused), ("$dynamicRef", ("#", "#/$defs/page", *refused))):
            for reference in references:
                external = json.loads(json.dumps(reviewed.machine_contract))
                schema = external["actions"][0]["input_schema"]
                schema["$defs"] = {"page": {"type": "integer", "default": {"type": "string"}}}
                schema["default"] = {"$ref": "https://example.test/schema.json"}
                schema["properties"]["page"] = {keyword: reference}
                with (
                    self.subTest(keyword=keyword, reference=reference),
                    self.assertRaisesRegex(assistant_manifest.ManifestError, "root or a named definition"),
                ):
                    assistant_manifest.parse_machine_contract(
                        json.dumps(external).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
                    )

        for definitions, reference in (
            ("$defs", "#/$defs/page"),
            ("definitions", "#/definitions/page"),
            ("$defs", "#/$defs/a~1b~0c"),
        ):
            local = json.loads(json.dumps(reviewed.machine_contract))
            schema = local["actions"][0]["input_schema"]
            schema[definitions] = {"page": {"type": "integer"}, "a/b~c": {"type": "integer"}}
            schema["properties"]["page"] = {"$ref": reference}
            with self.subTest(reference=reference):
                parsed = assistant_manifest.parse_machine_contract(
                    json.dumps(local).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
                )
                self.assertEqual(parsed["actions"][0]["input_schema"]["properties"]["page"], {"$ref": reference})

    def test_machine_schema_refuses_a_dialect_switch_or_a_nested_identifier(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        draft_07 = "http://json-schema.org/draft-07/schema#"
        remote = {"$ref": "https://example.test/schema.json"}
        for place in ("root", "nested"):
            switched = json.loads(json.dumps(reviewed.machine_contract))
            schema = switched["actions"][0]["input_schema"]
            node = {"type": "object", "additionalProperties": False, "dependencies": {"page": remote}}
            if place == "root":
                schema.update({"$schema": draft_07, "dependencies": {"page": remote}})
                schema["properties"]["page"] = {"$ref": "#"}
            else:
                schema["properties"]["page"] = {"$schema": draft_07, **node}
            with (
                self.subTest(place=place),
                self.assertRaisesRegex(assistant_manifest.ManifestError, "Draft 2020-12 dialect"),
            ):
                assistant_manifest.parse_machine_contract(
                    json.dumps(switched).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
                )

        rebound = json.loads(json.dumps(reviewed.machine_contract))
        rebound["actions"][0]["input_schema"]["properties"]["page"] = {
            "$id": "https://json-schema.org/draft/2020-12/meta/validation",
            "$ref": "#/$defs/simpleTypes",
        }
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "nested identifier"):
            assistant_manifest.parse_machine_contract(
                json.dumps(rebound).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
            )

        current = json.loads(json.dumps(reviewed.machine_contract))
        schema = current["actions"][0]["input_schema"]
        schema.update({"$schema": action_schema._DRAFT_2020_12, "$id": "https://example.test/action.json"})
        schema["properties"]["page"] = {"$schema": action_schema._DRAFT_2020_12, "type": "integer"}
        parsed = assistant_manifest.parse_machine_contract(
            json.dumps(current).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
        )
        self.assertEqual(parsed["actions"][0]["input_schema"]["$id"], "https://example.test/action.json")

    def test_machine_schema_reads_references_only_at_schema_nodes(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        remote = {"$ref": "https://example.test/schema.json"}
        data = json.loads(json.dumps(reviewed.machine_contract))
        schema = data["actions"][0]["input_schema"]
        # A property may be named "$ref", and instance data may carry a "$ref" key: neither is a reference.
        schema["properties"]["$ref"] = {"type": "string"}
        schema["properties"]["mode"] = {"const": remote, "enum": [remote], "default": remote, "examples": [remote]}
        parsed = assistant_manifest.parse_machine_contract(
            json.dumps(data).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
        )
        self.assertEqual(parsed["actions"][0]["input_schema"]["properties"]["$ref"], {"type": "string"})
        self.assertEqual(parsed["actions"][0]["input_schema"]["properties"]["mode"]["const"], remote)

        nested = json.loads(json.dumps(reviewed.machine_contract))
        nested["actions"][0]["input_schema"]["properties"]["pages"] = {
            "type": "array",
            "items": {"anyOf": [{"type": "integer"}, {"not": {"contentSchema": remote}}]},
        }
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "root or a named definition"):
            assistant_manifest.parse_machine_contract(
                json.dumps(nested).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY, allowed_hosts=()
            )

    def test_action_schema_validators_never_retrieve_a_uri(self) -> None:
        schema = {
            "type": "object",
            "properties": {"page": {"$ref": "https://example.test/schema.json"}},
            "additionalProperties": False,
        }
        with (
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("retrieved a URI")) as urlopen,
            self.assertRaises(ValueError),
        ):
            assistant_manifest.validate_schema_payload(assistant_manifest.action_schema_validator(schema), {"page": 1})
        urlopen.assert_not_called()

    def test_machine_schema_refuses_a_missing_or_cyclic_reference(self) -> None:
        closed = {"type": "object", "additionalProperties": False}
        for schema in (
            {**closed, "$defs": {"a": {"$ref": "#/$defs/a"}}, "properties": {"x": {"$ref": "#/$defs/a"}}},
            {**closed, "$ref": "#"},
            {**closed, "properties": {"self": {"$ref": "#"}}},
            {
                **closed,
                "$defs": {"node": {**closed, "properties": {"next": {"$ref": "#/definitions/link"}}}},
                "definitions": {"link": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/node"}]}},
            },
            {**closed, "$defs": {"a": {"type": "string"}}, "properties": {"x": {"$ref": "#/$defs/b"}}},
            {**closed, "definitions": {"a": {"type": "string"}}, "properties": {"x": {"$ref": "#/$defs/a"}}},
        ):
            with (
                self.subTest(schema=schema),
                self.assertRaisesRegex(assistant_manifest.ManifestError, "every reference without a cycle"),
            ):
                assistant_manifest._machine_schema(schema, kind="input")
            self.assertIsNone(action_schema.expanded_subschemas(schema))

    def test_machine_schema_bounds_validation_work_with_every_reference_expanded(self) -> None:
        def doubling(levels: int) -> dict[str, object]:
            definitions: dict[str, object] = {"d0": {"type": "string"}}
            for level in range(1, levels + 1):
                definitions[f"d{level}"] = {"allOf": [{"$ref": f"#/$defs/d{level - 1}"}] * 2}
            return {
                "type": "object",
                "additionalProperties": False,
                "$defs": definitions,
                "properties": {"a": {"$ref": f"#/$defs/d{levels}"}},
            }

        # 189 JSON values whose validation of `{}` alone would visit about 2^33 subschemas.
        attack = doubling(30)
        self.assertTrue(action_schema.json_nodes_within(attack, 189))
        self.assertEqual(action_schema.expanded_subschemas(attack), 12_884_901_791)
        started = time.perf_counter()
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "too large once its references are expanded"):
            assistant_manifest._machine_schema(attack, kind="input")
        self.assertLess(time.perf_counter() - started, 1.0)
        # Eight levels expand to 3,041 subschemas; a ninth would exceed the bound.
        self.assertEqual(action_schema.expanded_subschemas(doubling(8)), 3_041)
        self.assertEqual(assistant_manifest._machine_schema(doubling(8), kind="input")["type"], "object")
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "too large once its references are expanded"):
            assistant_manifest._machine_schema(doubling(9), kind="input")
        # A definition shared by several positions counts once per position.
        shared = {
            "type": "object",
            "additionalProperties": False,
            "$defs": {"a/b~": {"type": "string"}},
            "properties": {"x": {"$ref": "#/$defs/a~1b~0"}, "y": {"$ref": "#/$defs/a~1b~0"}},
        }
        self.assertEqual(action_schema.expanded_subschemas(shared), 7)

    def test_every_real_action_schema_expands_to_its_literal_subschemas(self) -> None:
        contracts = [reviewed.machine_contract for reviewed in _reviewed_catalog().values()]
        contracts.append(json.loads(REFERENCE_CONTRACT.read_bytes()))
        schemas = [
            action[position]
            for contract in contracts
            for action in contract["actions"]
            for position in (
                "input_schema",
                "output_schema",
            )
        ]
        self.assertGreater(len(schemas), 2)
        for schema in schemas:
            with self.subTest(schema=sorted(schema)):
                expanded = action_schema.expanded_subschemas(schema)
                self.assertIsNotNone(expanded)
                # Without references, every counted subschema is one of the literal JSON values.
                self.assertFalse(action_schema.json_nodes_within(schema, expanded - 1))
                self.assertLessEqual(expanded, action_schema.MAX_EXPANDED_SUBSCHEMAS)
                self.assertEqual(assistant_manifest._machine_schema(schema, kind="output"), schema)

    def test_payload_validation_still_fails_closed_on_an_unadmitted_reference_cycle(self) -> None:
        schema = {"type": "object", "$ref": "#", "additionalProperties": False}
        with self.assertRaisesRegex(ValueError, "does not match its reviewed schema") as raised:
            assistant_manifest.validate_schema_payload(assistant_manifest.action_schema_validator(schema), {})
        self.assertIsInstance(raised.exception.__cause__, RecursionError)

    def test_machine_schema_reuses_only_exact_plain_json_validation(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "description": "exact-cache-security-test",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        }
        action_schema._check_schema_json.cache_clear()
        check_schema = assistant_manifest.Draft202012Validator.check_schema
        try:
            with mock.patch.object(
                assistant_manifest.Draft202012Validator, "check_schema", wraps=check_schema
            ) as checked:
                self.assertEqual(assistant_manifest._machine_schema(schema, kind="input"), schema)
                self.assertEqual(
                    assistant_manifest._machine_schema(json.loads(json.dumps(schema)), kind="input"), schema
                )
                self.assertEqual(checked.call_count, 1)

                with self.assertRaisesRegex(assistant_manifest.ManifestError, "is invalid"):
                    assistant_manifest._machine_schema({**schema, "required": ("value",)}, kind="input")
                self.assertEqual(checked.call_count, 2)

                numeric_key = {**schema, "properties": {1: {"type": "string"}}}
                self.assertEqual(assistant_manifest._machine_schema(numeric_key, kind="input"), numeric_key)
                self.assertEqual(checked.call_count, 3)

                schema["properties"] = "invalid"
                with self.assertRaisesRegex(assistant_manifest.ManifestError, "is invalid"):
                    assistant_manifest._machine_schema(schema, kind="input")
                self.assertEqual(checked.call_count, 4)
        finally:
            action_schema._check_schema_json.cache_clear()

    def test_deep_machine_schema_fails_closed_on_validator_recursion(self) -> None:
        schema: dict[str, object] = {"type": "object", "additionalProperties": False}
        for _ in range(100):
            schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {"child": schema},
            }
        json.dumps(schema)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "is invalid") as raised:
            assistant_manifest._machine_schema(schema, kind="input")
        self.assertIsInstance(raised.exception.__cause__, RecursionError)


if __name__ == "__main__":
    unittest.main()
