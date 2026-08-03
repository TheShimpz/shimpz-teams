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
        'genesis = "Use the available Powers."\n'
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

    def test_update_authority_allows_only_equal_or_narrower_manifest_contracts(self) -> None:
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

        self.assertTrue(assistant_manifest.update_preserves_authority(previous, previous))
        self.assertTrue(assistant_manifest.update_preserves_authority(previous, narrowing))
        self.assertFalse(assistant_manifest.update_preserves_authority(narrowing, widened_host))
        self.assertFalse(assistant_manifest.update_preserves_authority(narrowing, widened_scope))
        self.assertFalse(assistant_manifest.update_preserves_authority(no_integration, added_integration))
        self.assertTrue(assistant_manifest.update_preserves_authority(added_integration, no_integration))

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
            b'[powers.lookup]\nsummary = "Lookup."\n',
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
            manifest().replace(b'genesis = "Use the available Powers."', b'genesis = ""'),
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
            set(reviewed.machine_contract["powers"][0]),
            {"id", "input_schema", "output_schema", "integrations"},
        )

        foreign = json.loads(raw)
        foreign["powers"][0]["integrations"] = ["github"]
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
            sum(len(assistant.powers) for assistant in catalog.values()) * 2,
        )
        validator = reviewed.power_validators["list-zones"]["input"]
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
        malformed["powers"][0]["input_schema"] = {"type": "not-a-json-schema-type"}

        for raw in (
            json.dumps(malformed).encode(),
            b'{"version":1,"version":1,"powers":[]}',
            b"x" * (assistant_manifest.MAX_CONTRACT_BYTES + 1),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_machine_contract(raw, reviewed.integrations)

    def test_machine_contract_loader_rejects_open_top_level_and_nested_schemas(self) -> None:
        reviewed = _reviewed_catalog()["shimpz-cloudflare"]
        open_contracts = []
        for schema_name in ("input_schema", "output_schema"):
            contract = json.loads(json.dumps(reviewed.machine_contract))
            contract["powers"][0][schema_name].pop("additionalProperties")
            open_contracts.append((schema_name, contract))
        nested = json.loads(json.dumps(reviewed.machine_contract))
        nested["powers"][0]["output_schema"]["properties"]["pagination"].pop("additionalProperties")
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
        typeless["powers"][0]["input_schema"]["properties"]["page"] = {"properties": {"value": {"type": "string"}}}
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "must close every object"):
            assistant_manifest.parse_machine_contract(json.dumps(typeless).encode(), reviewed.integrations)

        boolean = json.loads(json.dumps(reviewed.machine_contract))
        boolean["powers"][0]["input_schema"]["properties"]["page"] = True
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.parse_machine_contract(json.dumps(boolean).encode(), reviewed.integrations)

        literals = json.loads(json.dumps(reviewed.machine_contract))
        literals["powers"][0]["input_schema"]["properties"].update(
            {
                "flag": {"type": "boolean", "enum": [True, False]},
                "choice": {"enum": [True, False]},
                "fixed": {"const": True},
            }
        )
        parsed = assistant_manifest.parse_machine_contract(json.dumps(literals).encode(), reviewed.integrations)

        self.assertEqual(
            parsed["powers"][0]["input_schema"]["properties"]["flag"],
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
        drifted["powers"][0]["id"] = "other"
        with self.assertRaises(assistant_manifest.ManifestError):
            cache.get(container, reviewed.integrations, drifted)


if __name__ == "__main__":
    unittest.main()
