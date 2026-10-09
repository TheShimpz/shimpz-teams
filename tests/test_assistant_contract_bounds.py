"""The JSON value bounds of each Action schema and of one whole machine contract."""

import unittest
from unittest import mock

from assistant import action_schema
from assistant import manifest as assistant_manifest
from tests import catalog_fixtures


class MachineContractBoundTests(unittest.TestCase):
    def test_machine_contract_bounds_every_json_value_of_each_schema_and_the_whole_contract(self) -> None:
        def schema(nodes: int) -> dict[str, object]:
            # Root, type, additionalProperties, and the default and const containers are five values; nested rows of
            # three values and scalar literals fill the rest.
            data = nodes - 5
            return {
                "type": "object",
                "additionalProperties": False,
                "default": [{"a": [None]}] * (data // 3),
                "const": [0] * (data % 3),
            }

        def contract(input_nodes: int, output_nodes: int, actions: int = 1) -> dict[str, object]:
            return {
                "version": 1,
                "actions": [
                    {
                        "id": f"run-{index}",
                        "description": catalog_fixtures.ACTION_DESCRIPTION,
                        "input_schema": schema(input_nodes),
                        "output_schema": schema(output_nodes),
                        "integrations": [],
                        "stored_inputs": [],
                        "input_files": [],
                        "human_requests": [],
                        "effect": "read_only",
                    }
                    for index in range(actions)
                ],
                "messages": sorted(
                    (
                        catalog_fixtures.message(catalog_fixtures.SUMMARY, 80),
                        catalog_fixtures.message(catalog_fixtures.ASSISTANT_DESCRIPTION, 500),
                        catalog_fixtures.message(catalog_fixtures.ACTION_DESCRIPTION, 120),
                    ),
                    key=lambda message: message["id"],
                ),
            }

        limit = 4096
        for input_nodes, output_nodes in ((limit, 5), (5, limit)):
            admitted = assistant_manifest.canonical_machine_contract(
                contract(input_nodes, output_nodes),
                (),
                **catalog_fixtures.COPY,
                allowed_hosts=(),
            )
            self.assertEqual(len(admitted["actions"]), 1)
        for input_nodes, output_nodes, kind in ((limit + 1, 5, "input"), (5, limit + 1, "output")):
            with self.assertRaisesRegex(assistant_manifest.ManifestError, f"{kind} schema is too large"):
                assistant_manifest.canonical_machine_contract(
                    contract(input_nodes, output_nodes),
                    (),
                    **catalog_fixtures.COPY,
                    allowed_hosts=(),
                )
        # The bound is checked before any metaschema work, so excess data in an otherwise invalid schema is refused
        # as too large.
        invalid = {**schema(limit + 1), "required": "invalid"}
        with (
            mock.patch.object(action_schema.Draft202012Validator, "check_schema") as check_schema,
            self.assertRaisesRegex(assistant_manifest.ManifestError, "too large"),
        ):
            assistant_manifest._machine_schema(invalid, kind="input")
        check_schema.assert_not_called()

        # Eight Actions of two schemas each: four contract values, five values of each of the three display messages,
        # eight values per Action, thirteen schemas of 2,043 values, and three of 2,042 make 32,768 values, with every
        # schema below its own bound.
        whole = contract(2043, 2043, actions=8)
        for index in (1, 2, 3):
            whole["actions"][index]["output_schema"] = schema(2042)
        self.assertEqual(4 + 3 * 5 + 8 * 8 + 13 * 2043 + 3 * 2042, 32_768)
        self.assertEqual(
            len(
                assistant_manifest.canonical_machine_contract(whole, (), **catalog_fixtures.COPY, allowed_hosts=())[
                    "actions"
                ]
            ),
            8,
        )
        whole["actions"][0]["output_schema"]["const"].append(0)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "machine contract is too large"):
            assistant_manifest.canonical_machine_contract(whole, (), **catalog_fixtures.COPY, allowed_hosts=())

        self.assertTrue(action_schema.json_nodes_within({"a": [1, ("b",)]}, 5))
        self.assertFalse(action_schema.json_nodes_within({"a": [1, ("b",)]}, 4))


if __name__ == "__main__":
    unittest.main()
