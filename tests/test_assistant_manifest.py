from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant import manifest as assistant_manifest

FIXTURE_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "reference-assistant" / "shimpz.toml"


def _reviewed_catalog(assistant_id: str = "shimpz-cloudflare"):
    catalog = {
        "version": 1,
        "assistants": {
            assistant_id: {
                "name": "Shimpz Cloudflare",
                "summary": "Cloudflare contract test fixture",
                "allowed_hosts": ["api.cloudflare.com"],
                "integrations": {
                    "cloudflare": {
                        "scopes": ["zone.read", "dns.read", "offline_access"],
                    }
                },
                "contract": json.loads((FIXTURE_MANIFEST.parent / "shimpz.contract.json").read_text(encoding="utf-8")),
            }
        },
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "catalog.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        return assistant_manifest.load_reviewed_catalog(path)


def manifest(
    *,
    allowed_hosts: tuple[str, ...] = ("api.example.com",),
    integrations: str = "",
    name: str = "Fixture Assistant",
    summary: str = "Exercise immutable admission.",
    creators: str = '["@fixture"]',
    github: str = "https://github.com/TheShimpz/fixture-assistant",
) -> bytes:
    hosts = ", ".join(f'"{host}"' for host in allowed_hosts)
    return (
        "[shimpz]\n"
        "spec = 1\n"
        'id = "fixture-assistant"\n'
        'version = "0.1.0"\n'
        f'name = "{name}"\n'
        f'summary = "{summary}"\n'
        f"creators = {creators}\n"
        f'github = "{github}"\n'
        'genesis = "Use the available Actions."\n'
        "\n[network]\n"
        f"allowed_hosts = [{hosts}]\n\n{integrations}"
    ).encode()


def archive(
    content: bytes,
    *,
    name: str = "shimpz.toml",
    member_type: bytes | None = None,
    mode: int = 0o444,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        member.mode = mode
        if member_type is not None:
            member.type = member_type
        bundle.addfile(member, io.BytesIO(content))
    return output.getvalue()


class Container:
    def __init__(self, container_id: str, content: bytes) -> None:
        self.id = container_id
        self.content = content
        self.reads = 0

    def get_archive(self, path: str):
        self.reads += 1
        if path != assistant_manifest.MANIFEST_PATH:
            raise AssertionError(f"unexpected archive path: {path}")
        payload = archive(self.content)
        return (
            iter((payload[:113], payload[113:])),
            {"name": "shimpz.toml", "size": len(self.content), "mode": 0o444},
        )


class ContractContainer:
    def __init__(self, container_id: str, content: bytes) -> None:
        self.id = container_id
        self.content = content
        self.reads = 0

    def get_archive(self, path: str):
        self.reads += 1
        if path != assistant_manifest.CONTRACT_PATH:
            raise AssertionError(f"unexpected archive path: {path}")
        payload = archive(self.content, name="shimpz.contract.json")
        return (
            iter((payload,)),
            {"name": "shimpz.contract.json", "size": len(self.content), "mode": 0o444},
        )


class AssistantManifestTests(unittest.TestCase):
    def test_reads_the_sdk_baked_v1_manifest_path(self) -> None:
        self.assertEqual(assistant_manifest.MANIFEST_PATH, "/opt/shimpz/shimpz.toml")

    def test_reference_fixture_matches_the_reviewed_cloudflare_security_intent(self) -> None:
        declared = assistant_manifest.parse_manifest_contract(FIXTURE_MANIFEST.read_bytes())
        reviewed_assistant = _reviewed_catalog()["shimpz-cloudflare"]
        reviewed = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=reviewed_assistant.allowed_hosts,
            integrations={integration.id: integration for integration in reviewed_assistant.integrations},
        )

        self.assertEqual(declared, reviewed)

    def test_automatic_update_allows_oauth_changes_but_not_new_outbound_hosts(self) -> None:
        previous = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=("api.cloudflare.com", "api.example.com"),
            integration_declarations={
                "cloudflare": ("zone.read", "dns.read", "offline_access"),
            },
        )
        narrowing = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=("api.cloudflare.com",),
            integration_declarations={"cloudflare": ("zone.read",)},
        )
        widened_host = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=("api.cloudflare.com", "api.example.com", "api.openai.com"),
            integration_declarations={"cloudflare": ("zone.read",)},
        )
        widened_scope = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=("api.cloudflare.com",),
            integration_declarations={"cloudflare": ("zone.read", "dns.read", "offline_access")},
        )
        added_integration = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=("api.cloudflare.com",),
            integration_declarations={"cloudflare": ("zone.read",)},
        )
        no_integration = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=("api.cloudflare.com",),
        )

        self.assertTrue(assistant_manifest.automatic_update_preserves_egress(previous, previous))
        self.assertTrue(assistant_manifest.automatic_update_preserves_egress(previous, narrowing))
        self.assertFalse(assistant_manifest.automatic_update_preserves_egress(narrowing, widened_host))
        self.assertTrue(assistant_manifest.automatic_update_preserves_egress(narrowing, widened_scope))
        self.assertTrue(assistant_manifest.automatic_update_preserves_egress(no_integration, added_integration))
        self.assertTrue(assistant_manifest.automatic_update_preserves_egress(added_integration, no_integration))

    def test_reviewed_catalog_rejects_reserved_and_oversized_assistant_ids(self) -> None:
        invalid = (
            "postgres",
            "assistant-egress",
            "shimpz-assistant-egress",
            "a" * 41,
        )
        for assistant_id in invalid:
            with self.subTest(assistant_id=assistant_id), self.assertRaises(assistant_manifest.ManifestError):
                _reviewed_catalog(assistant_id)

    def test_reads_reduced_manifest_and_derives_provider_from_integration_id(self) -> None:
        content = manifest(
            allowed_hosts=("api.cloudflare.com",),
            integrations='[integrations.cloudflare]\nscopes = ["zone.read", "dns.read", "offline_access"]\n',
        )

        contract = assistant_manifest.read_container_manifest_contract(Container("container-one", content))

        self.assertEqual(contract.allowed_hosts, ("api.cloudflare.com",))
        self.assertEqual(
            contract.integrations,
            (
                assistant_manifest.IntegrationDeclaration(
                    "cloudflare",
                    "cloudflare",
                    ("dns.read", "offline_access", "zone.read"),
                ),
            ),
        )

    def test_integrations_are_optional(self) -> None:
        contract = assistant_manifest.parse_manifest_contract(manifest(allowed_hosts=()))

        self.assertEqual(contract.allowed_hosts, ())
        self.assertEqual(contract.integrations, ())

    def test_unsupported_manifest_fields_fail_closed(self) -> None:
        unsupported = (
            b"schema_version = 2\n",
            b'[actions.lookup]\nsummary = "Lookup."\n',
            b'[secrets.token]\nname = "Token"\nsummary = "Old."\n',
            b'[integrations.cloudflare]\nprovider = "cloudflare"\nscopes = ["zone.read"]\n',
        )

        for addition in unsupported:
            with self.subTest(addition=addition), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_manifest_contract(manifest() + addition)

    def test_retired_root_fields_and_network_inside_shimpz_fail_closed(self) -> None:
        retired_root = manifest().replace(b"[shimpz]\n", b"").replace(b"\n[network]\n", b"\n")
        network_inside_shimpz = manifest().replace(b"\n[network]\n", b"\n")

        for content in (retired_root, network_inside_shimpz):
            with self.subTest(content=content), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_manifest_contract(content)

    def test_unknown_provider_and_unreviewed_scopes_fail_closed(self) -> None:
        invalid = (
            '[integrations.github]\nscopes = ["repo.read"]\n',
            '[integrations.cloudflare]\nscopes = ["zone.write"]\n',
            '[integrations.cloudflare]\nscopes = ["zone.read", "zone.read"]\n',
            "[integrations.cloudflare]\nscopes = []\n",
        )

        for integrations in invalid:
            with self.subTest(integrations=integrations), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_manifest_contract(manifest(integrations=integrations))

    def test_public_metadata_is_required_and_bounded(self) -> None:
        invalid = (
            b'name = "Only a name"\n',
            manifest().replace(b"spec = 1", b"spec = 4"),
            manifest().replace(b'id = "fixture-assistant"', b'id = "Invalid"'),
            manifest().replace(b'version = "0.1.0"', b'version = "v1"'),
            manifest().replace(b'genesis = "Use the available Actions."', b'genesis = ""'),
            manifest(name=" Leading"),
            manifest(summary="line\nbreak"),
            manifest(creators="[]"),
            manifest(creators='["fixture"]'),
            manifest(github="http://github.com/TheShimpz/fixture"),
            manifest() + b'homepage = "https://example.com"\n',
        )

        for content in invalid:
            with self.subTest(content=content), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_manifest_contract(content)

    def test_unsafe_hosts_fail_closed(self) -> None:
        unsafe = (
            "*.example.com",
            "https://example.com",
            "example.com:443",
            "127.0.0.1",
            "localhost",
            "Example.com",
            "example.com.",
            "example..com",
            "tést.example",
            "api.example.test",
        )
        for host in unsafe:
            with self.subTest(host=host), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_manifest_contract(manifest(allowed_hosts=(host,)))

    def test_invalid_text_toml_size_and_credential_material_fail_closed(self) -> None:
        invalid = (
            b"",
            manifest() + b"\x00",
            b"\xff",
            b'name = "invalid',
            b"cloudflare" * (assistant_manifest.MAX_MANIFEST_BYTES + 1),
            manifest() + b'access_token = "credential-value-123456"\n',
        )
        for content in invalid:
            with self.subTest(size=len(content)), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_manifest_contract(content)

    def test_archive_shape_and_metadata_fail_closed(self) -> None:
        valid = manifest()
        invalid_cases = (
            (archive(valid, name="other.toml"), {"name": "shimpz.toml", "size": len(valid), "mode": 0o444}),
            (
                archive(valid, member_type=tarfile.SYMTYPE),
                {"name": "shimpz.toml", "size": len(valid), "mode": 0o444},
            ),
            (archive(valid, mode=0o644), {"name": "shimpz.toml", "size": len(valid), "mode": 0o444}),
            (archive(valid), {"name": "shimpz.toml", "size": len(valid), "mode": 0o100444}),
            (archive(valid), {"name": "shimpz.toml", "size": len(valid), "mode": 0o644}),
            (archive(valid), {"name": "shimpz.toml", "size": len(valid) + 1, "mode": 0o444}),
        )
        for payload, metadata in invalid_cases:
            with self.subTest(metadata=metadata), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.read_container_manifest_contract(
                    type(
                        "InvalidContainer",
                        (),
                        {
                            "get_archive": lambda _self, _path, value=(payload, metadata): (
                                iter((value[0],)),
                                value[1],
                            )
                        },
                    )()
                )

    def test_container_archive_transport_failure_is_unavailable(self) -> None:
        class UnavailableContainer:
            @staticmethod
            def get_archive(_path):
                raise RuntimeError("Docker transport failed")

        with self.assertRaises(assistant_manifest.ManifestUnavailableError):
            assistant_manifest.read_container_manifest_contract(UnavailableContainer())

    def test_cache_compares_reviewed_hosts_and_integrations_and_rejects_drift(self) -> None:
        content = manifest(
            allowed_hosts=("api.cloudflare.com",),
            integrations='[integrations.cloudflare]\nscopes = ["dns.read", "zone.read"]\n',
        )
        container = Container("container-one", content)
        cache = assistant_manifest.ManifestContractCache(max_entries=1)
        expected = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=("api.cloudflare.com",),
            integration_declarations={"cloudflare": ("zone.read", "dns.read")},
        )

        self.assertEqual(cache.get(container, expected), expected)
        self.assertEqual(cache.get(container, expected), expected)
        self.assertEqual(container.reads, 1)

        drifted = (
            assistant_manifest.canonical_manifest_contract(
                allowed_hosts=("api.github.com",),
                integration_declarations={"cloudflare": ("zone.read", "dns.read")},
            ),
            assistant_manifest.canonical_manifest_contract(
                allowed_hosts=("api.cloudflare.com",),
                integration_declarations={"cloudflare": ("zone.read",)},
            ),
        )
        for reviewed in drifted:
            with self.subTest(reviewed=reviewed), self.assertRaises(assistant_manifest.ManifestError):
                cache.get(container, reviewed)

    def test_machine_contract_loader_accepts_reviewed_artifact_and_rejects_foreign_integrations(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        raw = json.dumps(reviewed.machine_contract, separators=(",", ":")).encode()

        self.assertEqual(
            assistant_manifest.parse_machine_contract(raw, reviewed.integrations),
            reviewed.machine_contract,
        )
        self.assertEqual(
            set(reviewed.machine_contract["actions"][0]),
            {"id", "input_schema", "output_schema", "integrations", "human_requests"},
        )

        foreign = json.loads(raw)
        foreign["actions"][0]["integrations"] = ["github"]
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.parse_machine_contract(json.dumps(foreign).encode(), reviewed.integrations)

    def test_reviewed_catalog_precompiles_payload_validators(self) -> None:
        with mock.patch.object(
            assistant_manifest,
            "Draft202012Validator",
            wraps=assistant_manifest.Draft202012Validator,
        ) as validator_class:
            catalog = _reviewed_catalog()
            reviewed = catalog["shimpz-cloudflare"]

        self.assertEqual(
            validator_class.call_count,
            sum(len(assistant.actions) for assistant in catalog.values()) * 2,
        )
        validator = reviewed.action_validators["list-zones"]["input"]
        with (
            mock.patch.object(assistant_manifest, "_machine_schema") as canonicalize,
            mock.patch.object(assistant_manifest, "Draft202012Validator") as construct,
        ):
            self.assertEqual(
                assistant_manifest.validate_schema_payload(validator, {"page": 1, "per_page": 10}),
                {"page": 1, "per_page": 10},
            )
        canonicalize.assert_not_called()
        construct.assert_not_called()

    def test_machine_contract_loader_rejects_malformed_schema_and_oversized_artifact(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        malformed = json.loads(json.dumps(reviewed.machine_contract))
        malformed["actions"][0]["input_schema"] = {"type": "not-a-json-schema-type"}

        for raw in (
            json.dumps(malformed).encode(),
            b'{"version":1,"version":1,"actions":[]}',
            b"x" * (assistant_manifest.MAX_CONTRACT_BYTES + 1),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_machine_contract(raw, reviewed.integrations)

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
                assistant_manifest.parse_machine_contract(json.dumps(contract).encode(), reviewed.integrations)

    def test_machine_schema_closes_typeless_objects_and_rejects_boolean_subschemas(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]

        typeless = json.loads(json.dumps(reviewed.machine_contract))
        typeless["actions"][0]["input_schema"]["properties"]["page"] = {"properties": {"value": {"type": "string"}}}
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "must close every object"):
            assistant_manifest.parse_machine_contract(json.dumps(typeless).encode(), reviewed.integrations)

        boolean = json.loads(json.dumps(reviewed.machine_contract))
        boolean["actions"][0]["input_schema"]["properties"]["page"] = True
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.parse_machine_contract(json.dumps(boolean).encode(), reviewed.integrations)

        literals = json.loads(json.dumps(reviewed.machine_contract))
        literals["actions"][0]["input_schema"]["properties"].update(
            {
                "flag": {"type": "boolean", "enum": [True, False]},
                "choice": {"enum": [True, False]},
                "fixed": {"const": True},
            }
        )
        parsed = assistant_manifest.parse_machine_contract(json.dumps(literals).encode(), reviewed.integrations)

        self.assertEqual(
            parsed["actions"][0]["input_schema"]["properties"]["flag"],
            {"type": "boolean", "enum": [True, False]},
        )

    def test_machine_contract_cache_reads_once_and_requires_exact_review(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        raw = json.dumps(reviewed.machine_contract, separators=(",", ":")).encode()
        container = ContractContainer("machine-generation", raw)
        cache = assistant_manifest.MachineContractCache()

        self.assertEqual(
            cache.get(container, reviewed.integrations, reviewed.machine_contract),
            reviewed.machine_contract,
        )
        with mock.patch.object(
            assistant_manifest,
            "canonical_machine_contract",
            wraps=assistant_manifest.canonical_machine_contract,
        ) as canonicalize:
            self.assertEqual(
                cache.get(container, reviewed.integrations, reviewed.machine_contract),
                reviewed.machine_contract,
            )
        canonicalize.assert_not_called()
        self.assertEqual(container.reads, 1)

        drifted = json.loads(raw)
        drifted["actions"][0]["id"] = "other"
        with self.assertRaises(assistant_manifest.ManifestError):
            cache.get(container, reviewed.integrations, drifted)

    def test_public_contract_helpers_reject_wrong_shapes_and_secret_like_text(self) -> None:
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.canonical_allowed_hosts("api.example.com")
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "credential"):
            assistant_manifest._public_text(
                "api_key=private-material-123456",
                kind="summary",
                maximum=160,
            )
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.canonical_integration_declarations([])
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.automatic_update_preserves_egress(object(), object())
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.reviewed_manifest_contract(allowed_hosts=(), integrations=None)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "provider"):
            assistant_manifest.reviewed_manifest_contract(
                allowed_hosts=(),
                integrations={
                    "cloudflare": type("Metadata", (), {"provider": "x", "scopes": ("zone.read",)})(),
                },
            )
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "reviewed manifest"):
            assistant_manifest.reviewed_manifest_contract(
                allowed_hosts=(),
                integrations={"cloudflare": object()},
            )

    def test_machine_contract_shape_schema_and_usage_edges_fail_closed(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        valid = json.loads(json.dumps(reviewed.machine_contract))
        variants = []
        variants.append({})
        variants.append({"version": 1, "actions": []})
        malformed_action = json.loads(json.dumps(valid))
        malformed_action["actions"][0]["extra"] = True
        variants.append(malformed_action)
        duplicated = json.loads(json.dumps(valid))
        duplicated["actions"].append(json.loads(json.dumps(duplicated["actions"][0])))
        variants.append(duplicated)
        invalid_human = json.loads(json.dumps(valid))
        invalid_human["actions"][0]["human_requests"] = ["invalid"]
        variants.append(invalid_human)
        unused_integration = json.loads(json.dumps(valid))
        for action in unused_integration["actions"]:
            action["integrations"] = []
        variants.append(unused_integration)
        for contract in variants:
            with self.subTest(contract=contract), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.canonical_machine_contract(contract, reviewed.integrations)

        with self.assertRaisesRegex(assistant_manifest.ManifestError, "subschema"):
            assistant_manifest._reject_open_or_boolean_subschema([], kind="input")
        schema_with_list = {
            "type": "object",
            "additionalProperties": False,
            "oneOf": [
                {"type": "object", "additionalProperties": False},
            ],
        }
        self.assertEqual(assistant_manifest._machine_schema(schema_with_list, kind="input"), schema_with_list)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "invalid"):
            assistant_manifest._machine_schema(
                {"type": "object", "additionalProperties": False, "properties": "invalid"},
                kind="input",
            )
        oversized = {
            "type": "object",
            "additionalProperties": False,
            "description": "x" * (129 * 1024),
        }
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "too large"):
            assistant_manifest._machine_schema(oversized, kind="input")

    def test_reviewed_catalog_file_and_entry_shapes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.json"
            with self.assertRaisesRegex(assistant_manifest.ManifestError, "unavailable"):
                assistant_manifest.load_reviewed_catalog(missing)

            valid_contract = json.loads((FIXTURE_MANIFEST.parent / "shimpz.contract.json").read_text())
            valid_entry = {
                "name": "Assistant",
                "summary": "Reviewed Assistant.",
                "allowed_hosts": [],
                "integrations": {},
                "contract": valid_contract,
            }
            values = (
                {},
                {"version": 1, "assistants": {}},
                {"version": 1, "assistants": {"assistant": {}}},
                {
                    "version": 1,
                    "assistants": {"assistant": {**valid_entry, "integrations": []}},
                },
                {
                    "version": 1,
                    "assistants": {
                        "assistant": {
                            **valid_entry,
                            "integrations": {"cloudflare": {}},
                        }
                    },
                },
            )
            path = root / "catalog.json"
            for value in values:
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(assistant_manifest.ManifestError):
                    assistant_manifest.load_reviewed_catalog(path)

    def test_manifest_credential_nesting_and_section_shapes_fail_closed(self) -> None:
        nested: object = "safe"
        for _ in range(66):
            nested = [nested]
        for value, message in (
            (nested, "nesting"),
            ({1: "value"}, "invalid key"),
            ({"client_secret": "value"}, "forbidden"),
            ("Bearer private-material-123456", "credential material"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(assistant_manifest.ManifestError, message):
                assistant_manifest._reject_credential_material(value)

        invalid_sections = (
            b'[shimpz]\nvalue = "x"\n[network]\nallowed_hosts = []\n',
            b'[shimpz]\nspec = 1\n[network]\nvalue = "x"\n',
            b'shimpz = "invalid"\n[network]\nallowed_hosts = []\n',
        )
        for raw in invalid_sections:
            with self.subTest(raw=raw), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest._manifest_table(raw)
        root_integration = b'integrations = "invalid"\n' + manifest()
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "integration declarations"):
            assistant_manifest.parse_manifest_contract(root_integration)

    def test_bounded_archive_closes_stream_and_classifies_chunk_failures(self) -> None:
        class Chunks:
            def __init__(self, values: tuple[object, ...]) -> None:
                self.values = values
                self.closed = False

            def __iter__(self):
                return iter(self.values)

            def close(self) -> None:
                self.closed = True

        chunks = Chunks((b"one", b"two"))
        self.assertEqual(assistant_manifest._bounded_archive(chunks), b"onetwo")
        self.assertTrue(chunks.closed)
        for values, maximum, error in (
            (("invalid",), 10, assistant_manifest.ManifestError),
            ((b"too-large",), 1, assistant_manifest.ManifestError),
        ):
            with self.subTest(values=values), self.assertRaises(error):
                assistant_manifest._bounded_archive(Chunks(values), maximum)

        class Broken:
            def __iter__(self):
                raise OSError("offline")

        with self.assertRaises(assistant_manifest.ManifestUnavailableError):
            assistant_manifest._bounded_archive(Broken())

    def test_container_metadata_extraction_and_size_mismatch_fail_closed(self) -> None:
        valid = manifest()
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "metadata"):
            assistant_manifest.read_container_manifest_contract(
                type("Container", (), {"get_archive": lambda _self, _path: (iter(()), None)})()
            )

        bundle = mock.Mock()
        member = mock.Mock()
        member.name = "shimpz.toml"
        member.isreg.return_value = True
        member.size = len(valid)
        member.mode = 0o444
        bundle.getmembers.return_value = [member]
        bundle.extractfile.return_value = None
        bundle.__enter__ = mock.Mock(return_value=bundle)
        bundle.__exit__ = mock.Mock(return_value=False)
        container = Container("container", valid)
        with (
            mock.patch.object(assistant_manifest.tarfile, "open", return_value=bundle),
            self.assertRaisesRegex(assistant_manifest.ManifestError, "archive"),
        ):
            assistant_manifest.read_container_manifest_contract(container)

        extracted = mock.Mock()
        extracted.read.side_effect = OSError("offline")
        bundle.extractfile.return_value = extracted
        with (
            mock.patch.object(assistant_manifest.tarfile, "open", return_value=bundle),
            self.assertRaisesRegex(assistant_manifest.ManifestError, "archive"),
        ):
            assistant_manifest.read_container_manifest_contract(container)

        extracted.read.side_effect = None
        extracted.read.return_value = valid[:-1]
        with (
            mock.patch.object(assistant_manifest.tarfile, "open", return_value=bundle),
            self.assertRaisesRegex(assistant_manifest.ManifestError, "archive"),
        ):
            assistant_manifest.read_container_manifest_contract(container)

        payload = archive(valid)
        mismatch = type(
            "Container",
            (),
            {
                "get_archive": lambda _self, _path: (
                    iter((payload,)),
                    {"name": "shimpz.toml", "size": len(valid) - 1, "mode": 0o444},
                )
            },
        )()
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "archive"):
            assistant_manifest.read_container_manifest_contract(mismatch)

    def test_contract_caches_validate_identity_review_and_evict_old_generations(self) -> None:
        for cache_type in (assistant_manifest.ManifestContractCache, assistant_manifest.MachineContractCache):
            with self.subTest(cache=cache_type.__name__), self.assertRaises(ValueError):
                cache_type(0)

        expected = assistant_manifest.parse_manifest_contract(manifest())
        cache = assistant_manifest.ManifestContractCache(max_entries=1)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "identity"):
            cache.get(object(), expected)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "reviewed"):
            cache.get(Container("valid", manifest()), object())
        first = Container("first", manifest())
        second = Container("second", manifest())
        cache.get(first, expected)
        cache.get(second, expected)
        self.assertEqual(tuple(cache._entries), ("second",))
        cache.discard(None)

        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        raw = json.dumps(reviewed.machine_contract, separators=(",", ":")).encode()
        machine = assistant_manifest.MachineContractCache(max_entries=1)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "identity"):
            machine.get(object(), reviewed.integrations, reviewed.machine_contract)
        machine.get(ContractContainer("first", raw), reviewed.integrations, reviewed.machine_contract)
        machine.get(ContractContainer("second", raw), reviewed.integrations, reviewed.machine_contract)
        self.assertEqual(tuple(machine._entries), ("second",))
        machine.discard(None)


if __name__ == "__main__":
    unittest.main()
