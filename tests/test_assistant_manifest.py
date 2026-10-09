import io
import json
import tarfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from assistant import action_schema
from assistant import manifest as assistant_manifest
from tests import catalog_fixtures

FIXTURE_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "reference-assistant" / "shimpz.toml"
FIXTURE_SUMMARY = "List Cloudflare zones and inspect their DNS records through OAuth."
FIXTURE_DESCRIPTION = (
    "Browse the zones of your Cloudflare account and read the DNS records of each zone. Every Action only reads; "
    "nothing in your account changes."
)
# The admitted summary and description a machine contract's catalog must carry.
FIXTURE_COPY = {"summary": FIXTURE_SUMMARY, "description": FIXTURE_DESCRIPTION}


def _reviewed_contract() -> SimpleNamespace:
    """The reference Cloudflare Assistant's admitted contract, built only through Team's own admission functions."""
    integrations = assistant_manifest.canonical_integration_declarations(
        {"cloudflare": ["zone.read", "dns.read", "offline_access"]}
    )
    allowed_hosts = assistant_manifest.canonical_allowed_hosts(["api.cloudflare.com"])
    stored_inputs = assistant_manifest.canonical_stored_input_declarations({}, allowed_hosts)
    contract = json.loads((FIXTURE_MANIFEST.parent / "shimpz.contract.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        allowed_hosts=allowed_hosts,
        integrations=integrations,
        stored_inputs=stored_inputs,
        machine_contract=assistant_manifest.canonical_machine_contract(
            contract, integrations, stored_inputs, **FIXTURE_COPY, allowed_hosts=allowed_hosts
        ),
    )


def manifest(
    *,
    allowed_hosts: tuple[str, ...] = ("api.example.com",),
    integrations: str = "",
    name: str = "Fixture Assistant",
    summary: str = "Exercise immutable admission.",
    description: str = catalog_fixtures.ASSISTANT_DESCRIPTION,
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
        f'description = "{description}"\n'
        f"creators = {creators}\n"
        f'github = "{github}"\n'
        'genesis = "Use the available Actions."\n'
        "\n[network]\n"
        f"allowed_hosts = [{hosts}]\n\n{integrations}"
    ).encode()


def _stored(label: str, **placement: str) -> dict[str, str]:
    """One Stored Input declaration with its placement, by default a header on the fixture's host."""
    fields = {"host": "api.example.com", **placement}
    if "query" not in fields:
        fields.setdefault("header", "X-Api-Key")
    return {"kind": "password", "label": label, "description": f"{label} for the provider.", **fields}


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
    def test_the_summary_is_a_short_description_of_at_most_eighty_characters(self) -> None:
        for summary in ("s" * 80, "s" * 79 + "\U0001f44b"):
            with self.subTest(summary=summary):
                assistant_manifest.parse_manifest_contract(manifest(summary=summary))
                self.assertEqual(assistant_manifest._public_text(summary, kind="summary", maximum=80), summary)
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.parse_manifest_contract(manifest(summary="s" * 81))

    def test_manifest_admits_only_declarative_stored_input_metadata(self) -> None:
        raw = manifest(
            integrations=(
                "[stored_inputs.whatsapp-token]\n"
                'kind = "password"\n'
                'label = "WhatsApp token"\n'
                'description = "Token used to call the WhatsApp API."\n'
                'host = "api.example.com"\n'
                'header = "Authorization"\n'
                'scheme = "Bearer"\n'
            )
        )

        parsed = assistant_manifest.parse_manifest_contract(raw)

        self.assertEqual(
            parsed.stored_inputs,
            (
                assistant_manifest.StoredInputDeclaration(
                    id="whatsapp-token",
                    kind="password",
                    label="WhatsApp token",
                    description="Token used to call the WhatsApp API.",
                    host="api.example.com",
                    header="Authorization",
                    scheme="Bearer",
                ),
            ),
        )

        with self.assertRaisesRegex(assistant_manifest.ManifestError, "declarations are invalid"):
            assistant_manifest.canonical_stored_input_declarations(
                {f"token-{index}": _stored("Token") for index in range(assistant_manifest.MAX_STORED_INPUTS + 1)},
                ("api.example.com",),
            )

    def test_each_stored_input_has_one_placement_its_host_and_no_other_value_shares(self) -> None:
        """Placement admission (ADR-0106): one allowed host, one field Team does not own, one plain proof target."""
        hosts = ("api.example.com", "other.example.com")
        token = _stored("Token", header="Authorization", scheme="Bearer")
        admitted = assistant_manifest.canonical_stored_input_declarations(
            {"token": token, "secret": _stored("Secret", query="appsecret_proof", hmac="token")}, hosts
        )
        self.assertEqual([(item.id, item.hmac) for item in admitted], [("secret", "token"), ("token", None)])
        refused = (
            {"token": {**token, "host": "collector.example.org"}},
            {"token": {key: value for key, value in token.items() if key != "header"}},
            {"token": {**token, "query": "key"}},
            {"token": _stored("Token", query="key", scheme="Bearer")},
            {"token": _stored("Token", header="Content-Length")},
            {"token": _stored("Token", header="bad header")},
            {"token": _stored("Token", query="a&b")},
            {"token": token, "other": _stored("Other", header="authorization")},
            {"token": token, "secret": _stored("Secret", query="proof", hmac="missing")},
            {"token": token, "secret": _stored("Secret", query="proof", hmac="secret")},
            {"token": {**token, "hmac": "secret"}, "secret": _stored("Secret", query="proof", hmac="token")},
            {"token": token, "secret": _stored("Secret", query="proof", hmac="token", host="other.example.com")},
        )
        for declarations in refused:
            with self.subTest(declarations=declarations), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.canonical_stored_input_declarations(declarations, hosts)

    def test_an_integration_bearer_owns_authorization_on_its_provider_hosts(self) -> None:
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "overlap"):
            assistant_manifest.canonical_manifest_contract(
                allowed_hosts=["api.cloudflare.com"],
                integration_declarations={"cloudflare": ["dns.read"]},
                stored_input_declarations={
                    "token": _stored("Token", header="Authorization", host="api.cloudflare.com")
                },
            )

    def test_an_automatic_update_keeps_every_retained_placement(self) -> None:
        def contract(**placement: str) -> assistant_manifest.ManifestContract:
            return assistant_manifest.canonical_manifest_contract(
                allowed_hosts=["api.example.com", "other.example.com"],
                stored_input_declarations={"token": _stored("Token", **{"header": "Authorization", **placement})},
            )

        previous = contract()
        self.assertTrue(assistant_manifest.automatic_update_preserves_egress(previous, contract()))
        for changed in ({"host": "other.example.com"}, {"header": "X-Token"}, {"scheme": "Bearer"}):
            with self.subTest(changed=changed):
                self.assertFalse(assistant_manifest.automatic_update_preserves_egress(previous, contract(**changed)))

    def test_machine_contract_requires_password_capability_for_stored_input(self) -> None:
        declarations = assistant_manifest.canonical_stored_input_declarations(
            {"whatsapp-token": _stored("WhatsApp token")}, ("api.example.com",)
        )
        contract = {
            "version": 1,
            "actions": [
                {
                    "id": "send-message",
                    "description": catalog_fixtures.ACTION_DESCRIPTION,
                    "input_schema": {"type": "object", "additionalProperties": False},
                    "output_schema": {"type": "object", "additionalProperties": False},
                    "integrations": [],
                    "stored_inputs": ["whatsapp-token"],
                    "input_files": [],
                    "human_requests": ["input:password"],
                    "effect": "mutating",
                }
            ],
            "messages": catalog_fixtures.display_messages(labels=("WhatsApp token",)),
        }
        self.assertEqual(
            assistant_manifest.canonical_machine_contract(
                contract,
                (),
                declarations,
                **catalog_fixtures.COPY,
                allowed_hosts=(),
            ),
            contract,
        )
        contract["actions"][0]["human_requests"] = []
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "request is undeclared"):
            assistant_manifest.canonical_machine_contract(
                contract,
                (),
                declarations,
                **catalog_fixtures.COPY,
                allowed_hosts=(),
            )

    def test_an_action_may_use_several_declared_stored_inputs_as_one_sorted_list(self) -> None:
        declarations = assistant_manifest.canonical_stored_input_declarations(
            {
                f"key-{index}": _stored("Key", header=f"X-Key-{index}")
                for index in range(1, assistant_manifest.MAX_STORED_INPUTS + 1)
            },
            ("api.example.com",),
        )

        def contract(stored_inputs: list[str]) -> dict[str, object]:
            return {
                "version": 1,
                "actions": [
                    {
                        "id": "send-message",
                        "description": catalog_fixtures.ACTION_DESCRIPTION,
                        "input_schema": {"type": "object", "additionalProperties": False},
                        "output_schema": {"type": "object", "additionalProperties": False},
                        "integrations": [],
                        "stored_inputs": stored_inputs,
                        "input_files": [],
                        "human_requests": ["input:password"],
                        "effect": "mutating",
                    }
                ],
                "messages": catalog_fixtures.display_messages(labels=("Key",)),
            }

        def admit(stored_inputs: list[str]) -> dict[str, object]:
            return assistant_manifest.canonical_machine_contract(
                contract(stored_inputs),
                (),
                declarations,
                **catalog_fixtures.COPY,
                allowed_hosts=(),
            )

        for admitted in (["key-1", "key-2"], [f"key-{index}" for index in range(1, 9)]):
            with self.subTest(admitted=admitted):
                self.assertEqual(admit(admitted)["actions"][0]["stored_inputs"], admitted)
        refused = (
            ["key-2", "key-1"],
            ["key-1", "key-1"],
            ["key-1", "undeclared-key"],
            [f"key-{index}" for index in range(1, 9)] + ["key-9"],
            ["key-1", 2],
        )
        for stored_inputs in refused:
            with (
                self.subTest(stored_inputs=stored_inputs),
                self.assertRaisesRegex(assistant_manifest.ManifestError, "Stored Inputs are invalid"),
            ):
                admit(stored_inputs)

    def test_reads_the_sdk_baked_v1_manifest_path(self) -> None:
        self.assertEqual(assistant_manifest.MANIFEST_PATH, "/opt/shimpz/shimpz.toml")

    def test_reference_fixture_matches_the_reviewed_cloudflare_security_intent(self) -> None:
        declared = assistant_manifest.parse_manifest_contract(FIXTURE_MANIFEST.read_bytes())
        reviewed_assistant = _reviewed_contract()
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
            manifest(summary="s" * 81),
            manifest(summary="s" * 80 + "\U0001f44b"),
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
        reviewed = _reviewed_contract()
        raw = json.dumps(reviewed.machine_contract, separators=(",", ":")).encode()

        self.assertEqual(
            assistant_manifest.parse_machine_contract(raw, reviewed.integrations, **FIXTURE_COPY, allowed_hosts=()),
            reviewed.machine_contract,
        )
        self.assertEqual(
            set(reviewed.machine_contract["actions"][0]),
            {
                "id",
                "description",
                "input_schema",
                "output_schema",
                "integrations",
                "stored_inputs",
                "input_files",
                "human_requests",
                "effect",
            },
        )

        foreign = json.loads(raw)
        foreign["actions"][0]["integrations"] = ["github"]
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.parse_machine_contract(
                json.dumps(foreign).encode(),
                reviewed.integrations,
                **FIXTURE_COPY,
                allowed_hosts=(),
            )

    def test_a_compiled_payload_validator_is_reused_without_recompiling(self) -> None:
        reviewed = _reviewed_contract()
        [action] = [action for action in reviewed.machine_contract["actions"] if action["id"] == "list-zones"]
        validator = assistant_manifest.action_schema_validator(action["input_schema"])
        with (
            mock.patch.object(assistant_manifest, "_machine_schema") as canonicalize,
            mock.patch.object(assistant_manifest.action_schema, "payload_validator") as construct,
        ):
            self.assertEqual(
                assistant_manifest.validate_schema_payload(validator, {"page": 1, "per_page": 10}),
                {"page": 1, "per_page": 10},
            )
        canonicalize.assert_not_called()
        construct.assert_not_called()

    def test_machine_contract_loader_rejects_malformed_schema_and_oversized_artifact(self) -> None:
        reviewed = _reviewed_contract()
        malformed = json.loads(json.dumps(reviewed.machine_contract))
        malformed["actions"][0]["input_schema"] = {"type": "not-a-json-schema-type"}

        for raw in (
            json.dumps(malformed).encode(),
            b'{"version":1,"version":1,"actions":[]}',
            b"x" * (assistant_manifest.MAX_CONTRACT_BYTES + 1),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_machine_contract(
                    raw,
                    reviewed.integrations,
                    **FIXTURE_COPY,
                    allowed_hosts=(),
                )

    def test_machine_contract_cache_reads_once_and_requires_exact_review(self) -> None:
        reviewed = _reviewed_contract()
        raw = json.dumps(reviewed.machine_contract, separators=(",", ":")).encode()
        container = ContractContainer("machine-generation", raw)
        cache = assistant_manifest.MachineContractCache()

        self.assertEqual(
            cache.get(
                container,
                reviewed.integrations,
                reviewed.stored_inputs,
                reviewed.machine_contract,
                **FIXTURE_COPY,
                allowed_hosts=(),
            ),
            reviewed.machine_contract,
        )
        with mock.patch.object(
            assistant_manifest,
            "canonical_machine_contract",
            wraps=assistant_manifest.canonical_machine_contract,
        ) as canonicalize:
            self.assertEqual(
                cache.get(
                    container,
                    reviewed.integrations,
                    reviewed.stored_inputs,
                    reviewed.machine_contract,
                    **FIXTURE_COPY,
                    allowed_hosts=(),
                ),
                reviewed.machine_contract,
            )
        canonicalize.assert_not_called()
        self.assertEqual(container.reads, 1)

        drifted = json.loads(raw)
        drifted["actions"][0]["id"] = "other"
        with self.assertRaises(assistant_manifest.ManifestError):
            cache.get(
                container,
                reviewed.integrations,
                reviewed.stored_inputs,
                drifted,
                **FIXTURE_COPY,
                allowed_hosts=(),
            )

    def test_public_contract_helpers_reject_wrong_shapes_and_secret_like_text(self) -> None:
        with self.assertRaises(assistant_manifest.ManifestError):
            assistant_manifest.canonical_allowed_hosts("api.example.com")
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "credential"):
            assistant_manifest._public_text(
                "api_key=private-material-123456",
                kind="summary",
                maximum=80,
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
        reviewed = _reviewed_contract()
        valid = json.loads(json.dumps(reviewed.machine_contract))
        variants = []
        variants.append({})
        variants.append({"version": 1, "actions": [], "messages": valid["messages"]})
        malformed_action = json.loads(json.dumps(valid))
        malformed_action["actions"][0]["extra"] = True
        variants.append(malformed_action)
        duplicated = json.loads(json.dumps(valid))
        duplicated["actions"].append(json.loads(json.dumps(duplicated["actions"][0])))
        variants.append(duplicated)
        invalid_human = json.loads(json.dumps(valid))
        invalid_human["actions"][0]["human_requests"] = ["invalid"]
        variants.append(invalid_human)
        invalid_stored_input = json.loads(json.dumps(valid))
        invalid_stored_input["actions"][0]["stored_inputs"] = ["undeclared-token"]
        variants.append(invalid_stored_input)
        multiple_authorizations = json.loads(json.dumps(valid))
        multiple_authorizations["actions"][0]["human_requests"] = ["approval", "auth:password"]
        variants.append(multiple_authorizations)
        unused_integration = json.loads(json.dumps(valid))
        for action in unused_integration["actions"]:
            action["integrations"] = []
        variants.append(unused_integration)
        variants.append({key: value for key, value in valid.items() if key != "messages"})
        variants.append({**valid, "messages": []})
        variants.append({**valid, "messages": catalog_fixtures.messages("Another summary.")})
        for contract in variants:
            with self.subTest(contract=contract), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.canonical_machine_contract(
                    contract,
                    reviewed.integrations,
                    **FIXTURE_COPY,
                    allowed_hosts=(),
                )

        with self.assertRaisesRegex(action_schema.ActionSchemaError, "subschema"):
            action_schema._reject_open_or_boolean_subschema([])
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
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "is invalid"):
            assistant_manifest._machine_schema({**oversized, "properties": "invalid"}, kind="input")
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "is invalid"):
            assistant_manifest._machine_schema(
                {"type": "object", "additionalProperties": False, "const": float("nan")}, kind="input"
            )

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
        root_stored_inputs = b'stored_inputs = "invalid"\n' + manifest()
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "Stored Input declarations"):
            assistant_manifest.parse_manifest_contract(root_stored_inputs)

        with self.assertRaisesRegex(assistant_manifest.ManifestError, "version is invalid"):
            assistant_manifest.canonical_manifest_identity(
                assistant_id="assistant",
                version="v1",
                name="Assistant",
                summary="Assistant summary.",
            )

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
        self.assertEqual(tuple(cache._cache._entries), ("second",))
        cache.discard(None)

        reviewed = _reviewed_contract()
        raw = json.dumps(reviewed.machine_contract, separators=(",", ":")).encode()
        machine = assistant_manifest.MachineContractCache(max_entries=1)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "identity"):
            machine.get(
                object(),
                reviewed.integrations,
                reviewed.stored_inputs,
                reviewed.machine_contract,
                **FIXTURE_COPY,
                allowed_hosts=(),
            )
        machine.get(
            ContractContainer("first", raw),
            reviewed.integrations,
            reviewed.stored_inputs,
            reviewed.machine_contract,
            **FIXTURE_COPY,
            allowed_hosts=(),
        )
        machine.get(
            ContractContainer("second", raw),
            reviewed.integrations,
            reviewed.stored_inputs,
            reviewed.machine_contract,
            **FIXTURE_COPY,
            allowed_hosts=(),
        )
        self.assertEqual(tuple(machine._cache._entries), ("second",))
        machine.discard(None)


if __name__ == "__main__":
    unittest.main()
