"""The complete Action pin a compiled Routine step holds, and the Assistant scope pin (ADR-0092 section 3)."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import unittest
from unittest import mock

from assistant import spec as assistant_registry
from local.chat import segment as local_chat_segment
from local.chat.types import ActiveAssistant
from local.install.runtime import AssistantSpec
from routine import pin as routine_pin
from tests import catalog_fixtures

RECORD = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}
CREATED = {
    "type": "object",
    "properties": {"id": {"type": "string"}},
    "required": ["id"],
    "additionalProperties": False,
}
FIND_INPUT = {
    "type": "object",
    "properties": {"operation": {"type": "string"}},
    "required": ["operation"],
    "additionalProperties": False,
}
FIND_OUTPUT = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["occurred", "not_occurred", "inconclusive"]},
        "record": CREATED,
    },
    "required": ["outcome"],
    "additionalProperties": False,
}


def _contract() -> dict[str, object]:
    return {
        "version": 1,
        "actions": [
            {
                "id": "create-record",
                "input_schema": RECORD,
                "output_schema": CREATED,
                "integrations": ["cloudflare"],
                "stored_inputs": [],
                "input_files": [],
                "human_requests": ["approval"],
                "effect": "mutating",
                "idempotency": {
                    "provider": "api.cloudflare.com",
                    "key": {"location": "header", "name": "Idempotency-Key"},
                    "scope": "account",
                    "retention_seconds": 86_400,
                    "same_payload_required": True,
                },
                "verifier": {
                    "action": "find-record",
                    "input": {"operation": {"from": "operation_id"}},
                    "outcome": "/outcome",
                    "result": "/record",
                },
            },
            {
                "id": "find-record",
                "input_schema": FIND_INPUT,
                "output_schema": FIND_OUTPUT,
                "integrations": [],
                "stored_inputs": ["api-key"],
                "input_files": [],
                "human_requests": ["input:password"],
                "effect": "read_only",
            },
            {
                "id": "list-zones",
                "input_schema": FIND_INPUT,
                "output_schema": CREATED,
                "integrations": [],
                "stored_inputs": [],
                "input_files": [],
                "human_requests": [],
                "effect": "read_only",
            },
        ],
        "messages": catalog_fixtures.messages(),
    }


def _spec(**changes: object) -> AssistantSpec:
    spec = AssistantSpec(
        assistant_id="shimpz-cloudflare",
        version="1.2.3",
        name="Cloudflare",
        summary=catalog_fixtures.SUMMARY,
        image="registry.example/cloudflare@sha256:" + "a" * 64,
        actions={},
        allowed_hosts=("api.cloudflare.com",),
        required_image_labels=(
            ("org.shimpz.assistant.id", "shimpz-cloudflare"),
            ("org.shimpz.source.digest", "sha256:" + "b" * 64),
        ),
        integrations={"cloudflare": assistant_registry.IntegrationSpec("cloudflare", ("dns.edit",))},
        stored_inputs={"api-key": assistant_registry.StoredInputSpec("password", "API key", "The key.")},
        machine_contract=_contract(),
        pack_digest="sha256:" + "c" * 64,
    )
    return dataclasses.replace(spec, **changes)


def _action(spec: AssistantSpec, action_id: str) -> dict[str, object]:
    return next(action for action in spec.machine_contract["actions"] if action["id"] == action_id)


def _edited(edit) -> AssistantSpec:
    spec = _spec(machine_contract=copy.deepcopy(_contract()))
    edit(spec)
    return spec


class RoutinePinTests(unittest.TestCase):
    def test_the_pin_is_stable_and_names_one_rendered_action(self) -> None:
        pin = routine_pin.action_pin(_spec(), "create-record", "pt")
        self.assertRegex(pin, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertEqual(routine_pin.action_pin(_spec(), "create-record", "pt"), pin)
        self.assertNotEqual(routine_pin.action_pin(_spec(), "create-record", "en"), pin)
        self.assertNotEqual(routine_pin.action_pin(_spec(), "find-record", "pt"), pin)
        with self.assertRaisesRegex(routine_pin.PinError, "not declared"):
            routine_pin.action_pin(_spec(), "delete-record", "pt")
        for locale in ("pt-BR", "", "xx"):
            with self.subTest(locale=locale), self.assertRaisesRegex(routine_pin.PinError, "locale"):
                routine_pin.action_pin(_spec(), "create-record", locale)

    def test_every_member_that_decides_the_action_is_drift(self) -> None:
        baseline = routine_pin.action_pin(_spec(), "create-record", "pt")
        drifted = {
            "output schema": _edited(lambda spec: _action(spec, "create-record")["output_schema"].update(title="x")),
            "input schema": _edited(lambda spec: _action(spec, "create-record")["input_schema"].update(title="x")),
            "effect": _edited(lambda spec: _action(spec, "create-record").update(effect="read_only")),
            "verifier": _edited(lambda spec: _action(spec, "create-record")["verifier"].update(outcome="/record/id")),
            "verifier target": _edited(lambda spec: _action(spec, "find-record").update(human_requests=[])),
            "idempotency": _edited(
                lambda spec: _action(spec, "create-record")["idempotency"].update(retention_seconds=60)
            ),
            "human requests": _edited(lambda spec: _action(spec, "create-record").update(human_requests=[])),
            "integrations": _edited(lambda spec: _action(spec, "create-record").update(integrations=[])),
            "catalog": _edited(lambda spec: spec.machine_contract["messages"].pop()),
            "integration scopes": _spec(
                integrations={"cloudflare": assistant_registry.IntegrationSpec("cloudflare", ("dns.read",))}
            ),
            "stored input declaration": _spec(
                stored_inputs={"api-key": assistant_registry.StoredInputSpec("password", "Key", "The key.")}
            ),
            "image": _spec(image="registry.example/cloudflare@sha256:" + "d" * 64),
            "source": _spec(required_image_labels=(("org.shimpz.source.digest", "sha256:" + "e" * 64),)),
            "version": _spec(version="1.2.4"),
            "outbound hosts": _spec(allowed_hosts=("api.cloudflare.com", "www.cloudflare.com")),
            "language pack": _spec(pack_digest="sha256:" + "f" * 64),
        }
        for member, spec in drifted.items():
            with self.subTest(member=member):
                self.assertNotEqual(routine_pin.action_pin(spec, "create-record", "pt"), baseline)

    def test_an_unrelated_action_leaves_the_pin_but_not_the_assistant_scope(self) -> None:
        unrelated = _edited(lambda spec: _action(spec, "list-zones").update(effect="mutating"))
        self.assertEqual(
            routine_pin.action_pin(unrelated, "create-record", "pt"),
            routine_pin.action_pin(_spec(), "create-record", "pt"),
        )
        scope = routine_pin.assistant_pin(_spec(), "sha256:" + "0" * 64)
        self.assertRegex(scope, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertEqual(routine_pin.assistant_pin(_spec(), "sha256:" + "0" * 64), scope)
        self.assertNotEqual(routine_pin.assistant_pin(unrelated, "sha256:" + "0" * 64), scope)
        self.assertNotEqual(routine_pin.assistant_pin(_spec(), "sha256:" + "1" * 64), scope)
        unverified = _edited(lambda spec: _action(spec, "create-record").pop("verifier"))
        self.assertNotEqual(
            routine_pin.action_pin(unverified, "create-record", "pt"),
            routine_pin.action_pin(_spec(), "create-record", "pt"),
        )

    def test_the_scope_pin_digests_the_catalog_once_and_holds_every_action_pin(self) -> None:
        brain = "sha256:" + "0" * 64
        digest = routine_pin.catalog_validator.catalog_digest
        with mock.patch.object(routine_pin.catalog_validator, "catalog_digest", wraps=digest) as counted:
            scope = routine_pin.assistant_pin(_spec(), brain)
        counted.assert_called_once_with(_spec().machine_contract["messages"])
        # Byte for byte the scope of each Action's own complete pin in the fixed scope locale.
        document = {
            "format": routine_pin.SCOPE_FORMAT,
            "brain": brain,
            "actions": {
                action_id: routine_pin.action_pin(_spec(), action_id, routine_pin.SCOPE_LOCALE)
                for action_id in ("create-record", "find-record", "list-zones")
            },
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
        self.assertEqual(scope, "sha256:" + hashlib.sha256(encoded).hexdigest())

    def test_a_routine_scope_detects_drift_its_brain_contract_cannot_see(self) -> None:
        active = ActiveAssistant(_spec(), "container")
        scope = local_chat_segment.routine_scope(active, "Use the declared Action.")
        self.assertEqual(local_chat_segment.routine_scope(active, "Use the declared Action."), scope)
        self.assertNotEqual(local_chat_segment.routine_scope(active, "Other guidance."), scope)
        changed = _edited(lambda spec: _action(spec, "create-record")["output_schema"].update(title="x"))
        self.assertNotEqual(
            local_chat_segment.routine_scope(ActiveAssistant(changed, "container"), "Use the declared Action."), scope
        )


if __name__ == "__main__":
    unittest.main()
