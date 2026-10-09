"""Team admission of Action effect classes, verifiers, and provider idempotency (ADR-0092)."""

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from assistant import effect as action_effect
from assistant import manifest as assistant_manifest
from assistant import spec as assistant_spec
from tests import catalog_fixtures

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "vectors" / "action-effect.json"
CLOSED = {"type": "object", "properties": {}, "additionalProperties": False}
OUTCOME = {"type": "string", "enum": ["occurred", "not_occurred", "inconclusive"]}
IDEMPOTENCY = {
    "provider": "api.example.com",
    "key": {"location": "header", "name": "Idempotency-Key"},
    "scope": "account",
    "retention_seconds": 86_400,
    "same_payload_required": True,
}


def _stored_inputs(actions: object) -> tuple[assistant_manifest.StoredInputDeclaration, ...]:
    """Declare every Stored Input a vector's Actions name, so only the effect rules decide the case."""
    names = {
        name
        for action in actions
        if isinstance(action, dict) and isinstance(action.get("stored_inputs"), list)
        for name in action["stored_inputs"]
        if isinstance(name, str)
    }
    return assistant_manifest.canonical_stored_input_declarations(
        {
            name: {
                "kind": "password",
                "label": "Key",
                "description": catalog_fixtures.STORED_INPUT_HELP,
                "help_url": catalog_fixtures.HELP_URL,
                "host": "api.example.com",
                "header": f"x-{name}",
            }
            for name in names
        },
        ("api.example.com",),
    )


def _providers(actions: object) -> tuple[str, ...]:
    """Every valid idempotency provider a vector names, admitted as the manifest's outbound hosts."""
    hosts = set()
    for action in actions:
        declaration = action.get("idempotency") if isinstance(action, dict) else None
        provider = declaration.get("provider") if isinstance(declaration, dict) else None
        if isinstance(provider, str):
            try:
                hosts.update(assistant_manifest.canonical_allowed_hosts([provider]))
            except assistant_manifest.ManifestError:
                continue
    return tuple(sorted(hosts))


def _contract(*actions: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "actions": list(actions), "messages": catalog_fixtures.display_messages(actions, ("Key",))}


def _action(action_id: str, effect: str, **members: object) -> dict[str, object]:
    return {
        "id": action_id,
        "description": catalog_fixtures.ACTION_DESCRIPTION,
        "input_schema": copy.deepcopy(CLOSED),
        "output_schema": copy.deepcopy(CLOSED),
        "integrations": [],
        "stored_inputs": [],
        "input_files": [],
        "human_requests": [],
        "effect": effect,
        **members,
    }


def _admit(contract: dict[str, object], allowed_hosts: tuple[str, ...] = ()) -> dict[str, object]:
    actions = contract["actions"]
    return assistant_manifest.canonical_machine_contract(
        contract,
        (),
        _stored_inputs(actions),
        summary=catalog_fixtures.SUMMARY,
        description=catalog_fixtures.ASSISTANT_DESCRIPTION,
        allowed_hosts=allowed_hosts,
    )


def _verified_pair() -> tuple[dict[str, object], dict[str, object]]:
    create = _action(
        "create-record",
        "mutating",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    )
    find = _action(
        "find-record",
        "read_only",
        input_schema={
            "type": "object",
            "properties": {"operation": {"type": "string"}},
            "required": ["operation"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"outcome": OUTCOME, "record": create["output_schema"]},
            "required": ["outcome"],
            "additionalProperties": False,
        },
    )
    create["verifier"] = {
        "action": "find-record",
        "input": {"operation": {"from": "operation_id"}},
        "outcome": "/outcome",
        "result": "/record",
    }
    return create, find


class EffectAdmissionTests(unittest.TestCase):
    def test_team_admission_matches_every_published_effect_vector(self) -> None:
        vectors = json.loads(VECTORS.read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            actions = case["actions"]
            items = actions if isinstance(actions, list) else []
            try:
                _admit(_contract(*items) | {"actions": actions}, _providers(items))
            except assistant_manifest.ManifestError:
                admitted = False
            else:
                admitted = True
            self.assertEqual(admitted, case["valid"], case["name"])

    def test_canonical_actions_keep_every_declaration_without_aliasing_the_input(self) -> None:
        create, find = _verified_pair()
        create["idempotency"] = copy.deepcopy(IDEMPOTENCY)
        contract = _contract(find, create)
        admitted = _admit(contract, ("api.example.com",))
        by_id = {action["id"]: action for action in admitted["actions"]}
        self.assertEqual(by_id["create-record"]["effect"], "mutating")
        self.assertEqual(by_id["create-record"]["verifier"], create["verifier"])
        self.assertEqual(by_id["create-record"]["idempotency"], IDEMPOTENCY)
        self.assertEqual(set(by_id["find-record"]) - {"effect"}, set(_action("x", "read_only")) - {"effect"})
        create["verifier"]["input"]["operation"]["from"] = "input"
        create["idempotency"]["key"]["name"] = "Changed"
        self.assertEqual(by_id["create-record"]["verifier"]["input"]["operation"], {"from": "operation_id"})
        self.assertEqual(by_id["create-record"]["idempotency"]["key"]["name"], "Idempotency-Key")
        spec = assistant_spec.action_spec(by_id["create-record"])
        self.assertEqual(
            (spec.effect, spec.verifier, spec.idempotency),
            ("mutating", by_id["create-record"]["verifier"], IDEMPOTENCY),
        )
        self.assertEqual(
            (assistant_spec.action_spec(by_id["find-record"]).verifier, assistant_spec.ActionSpec("", {}, {}).effect),
            (None, "mutating"),
        )

    def test_effect_members_are_required_closed_and_bounded(self) -> None:
        missing = _action("run", "read_only")
        del missing["effect"]
        unknown = _action("run", "read_only", retries=1)
        for action in (missing, unknown):
            with (
                self.subTest(action=sorted(action)),
                self.assertRaisesRegex(assistant_manifest.ManifestError, "Action"),
            ):
                _admit(_contract(action))
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "effect is invalid: effect_invalid"):
            _admit(_contract(_action("run", "destructive")))

    def test_an_idempotency_provider_must_be_an_exact_outbound_host_of_the_manifest(self) -> None:
        action = _action("run", "mutating", idempotency=copy.deepcopy(IDEMPOTENCY))
        for hosts in ((), ("example.com",), ("other.example.com", "api.example.org")):
            with (
                self.subTest(hosts=hosts),
                self.assertRaisesRegex(assistant_manifest.ManifestError, "idempotency_provider_undeclared"),
            ):
                _admit(_contract(action), hosts)
        self.assertEqual(_admit(_contract(action), ("api.example.com",))["actions"][0]["idempotency"], IDEMPOTENCY)

    def test_team_refuses_an_interactive_verifier_even_if_the_shared_validator_admitted_it(self) -> None:
        create, find = _verified_pair()
        self.assertEqual(_admit(_contract(create, find))["actions"][0]["verifier"]["action"], "find-record")
        interactive = (
            {"human_requests": ["approval"], "stored_inputs": []},
            {"human_requests": ["input:password"], "stored_inputs": []},
            {"human_requests": ["input:password", "input:text"], "stored_inputs": ["api-key"]},
        )
        for members in interactive:
            target = {**find, **members}
            with (
                self.subTest(members=members),
                mock.patch.object(action_effect.action_effect_validator, "effect_error", return_value=None),
                self.assertRaisesRegex(assistant_manifest.ManifestError, "verifier_interactive"),
            ):
                _admit(_contract(create, target))
        for stored_inputs in (["api-key"], ["api-key", "api-secret"]):
            keyed = {**find, "human_requests": ["input:password"], "stored_inputs": stored_inputs}
            with self.subTest(stored_inputs=stored_inputs):
                self.assertEqual(len(_admit(_contract(create, keyed))["actions"]), 2)

    def test_the_mirrored_validator_refuses_non_lists_and_values_it_cannot_compare(self) -> None:
        validator = action_effect.action_effect_validator
        self.assertEqual(validator.effect_error({"run": _action("run", "read_only")}), "actions_invalid")
        self.assertEqual(validator.effect_error(["run"]), "actions_invalid")
        self.assertFalse(validator._same(float("nan"), float("nan")))
        self.assertFalse(validator._same(object(), {}))


if __name__ == "__main__":
    unittest.main()
