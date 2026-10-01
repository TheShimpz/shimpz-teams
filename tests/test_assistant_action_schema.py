"""Machine-contract Action schema admission: closed objects, self-contained references, and bounded validation."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from test_assistant_manifest import FIXTURE_SUMMARY, _reviewed_catalog

from assistant import action_schema
from assistant import manifest as assistant_manifest


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
                    json.dumps(contract).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
                )

    def test_machine_schema_closes_typeless_objects_and_rejects_boolean_subschemas(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]

        typeless = json.loads(json.dumps(reviewed.machine_contract))
        typeless["actions"][0]["input_schema"]["properties"]["page"] = {"properties": {"value": {"type": "string"}}}
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "must close every object"):
            assistant_manifest.parse_machine_contract(
                json.dumps(typeless).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
            )

        boolean = json.loads(json.dumps(reviewed.machine_contract))
        boolean["actions"][0]["input_schema"]["properties"]["page"] = True
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.parse_machine_contract(
                json.dumps(boolean).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
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
            json.dumps(literals).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
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
                        json.dumps(external).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
                    )

        for definitions, reference in (
            ("$defs", "#/$defs/page"),
            ("definitions", "#/definitions/page"),
            ("$defs", "#/$defs/a~1b~0c"),
            ("$defs", "#"),
        ):
            local = json.loads(json.dumps(reviewed.machine_contract))
            schema = local["actions"][0]["input_schema"]
            schema[definitions] = {"page": {"type": "integer"}, "a/b~c": {"type": "integer"}}
            schema["properties"]["page"] = {"$ref": reference}
            with self.subTest(reference=reference):
                parsed = assistant_manifest.parse_machine_contract(
                    json.dumps(local).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
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
                    json.dumps(switched).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
                )

        rebound = json.loads(json.dumps(reviewed.machine_contract))
        rebound["actions"][0]["input_schema"]["properties"]["page"] = {
            "$id": "https://json-schema.org/draft/2020-12/meta/validation",
            "$ref": "#/$defs/simpleTypes",
        }
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "nested identifier"):
            assistant_manifest.parse_machine_contract(
                json.dumps(rebound).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
            )

        current = json.loads(json.dumps(reviewed.machine_contract))
        schema = current["actions"][0]["input_schema"]
        schema.update({"$schema": action_schema._DRAFT_2020_12, "$id": "https://example.test/action.json"})
        schema["properties"]["page"] = {"$schema": action_schema._DRAFT_2020_12, "type": "integer"}
        parsed = assistant_manifest.parse_machine_contract(
            json.dumps(current).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
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
            json.dumps(data).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
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
                json.dumps(nested).encode(), reviewed.integrations, summary=FIXTURE_SUMMARY
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

    def test_action_payload_is_refused_when_schema_references_recurse_without_end(self) -> None:
        for schema in (
            {
                "type": "object",
                "$defs": {"a": {"$ref": "#/$defs/a"}},
                "properties": {"x": {"$ref": "#/$defs/a"}},
                "additionalProperties": False,
            },
            {"type": "object", "$ref": "#", "additionalProperties": False},
        ):
            admitted = assistant_manifest._machine_schema(schema, kind="input")
            with (
                self.subTest(schema=schema),
                self.assertRaisesRegex(ValueError, "does not match its reviewed schema") as raised,
            ):
                assistant_manifest.validate_schema_payload(
                    assistant_manifest.action_schema_validator(admitted), {"x": 1}
                )
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
