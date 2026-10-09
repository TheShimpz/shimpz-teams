"""Team admission of Action file input declarations (ADR-0093)."""

import json
import unittest
from pathlib import Path

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_spec
from tests import catalog_fixtures

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "vectors" / "input-file.json"


def _admit(actions: object) -> dict[str, object]:
    return assistant_manifest.canonical_machine_contract(
        {"version": 1, "actions": actions, "messages": catalog_fixtures.messages()},
        (),
        summary=catalog_fixtures.SUMMARY,
        allowed_hosts=(),
    )


class InputFileAdmissionTests(unittest.TestCase):
    def test_team_admission_matches_every_published_input_file_vector(self) -> None:
        vectors = json.loads(VECTORS.read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            with self.subTest(case=case["name"]):
                try:
                    _admit(case["actions"])
                except assistant_manifest.ManifestError:
                    admitted = False
                else:
                    admitted = True
                self.assertEqual(admitted, case["valid"])

    def test_the_admitted_declaration_reaches_the_action_spec(self) -> None:
        file_case = next(
            case
            for case in json.loads(VECTORS.read_bytes())["cases"]
            if case["name"] == "one file input behind approval"
        )
        admitted = _admit(file_case["actions"])
        action = admitted["actions"][0]
        self.assertEqual(action["input_files"], ["document"])
        self.assertEqual(assistant_spec.action_spec(action).input_files, ("document",))


if __name__ == "__main__":
    unittest.main()
