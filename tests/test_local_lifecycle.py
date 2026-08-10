from __future__ import annotations

import copy
import sys
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase

from local import app as local_app
from local.assistant.egress import ASSISTANT_EGRESS_ALIAS

LOOKUP_INPUT = {"page": 1, "per_page": 25}
LOOKUP_RESULT = {
    "zones": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
DNS_INPUT = {"zone_id": "a" * 32, "page": 1, "per_page": 25}
DNS_RESULT = {
    "records": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
TEST_SECRET_VALUES = {
    "service-token": "service-test-credential-123456789",
    "client-key": "client-key-test-credential-123456789",
    "client-secret": "client-secret-test-credential-123456789",
    "session-token": "session-token-test-credential-123456789",
    "session-secret": "session-secret-test-credential-123456789",
}
TEST_ACCOUNT_ACCESS_TOKEN = "-".join(("oauth", "access", "test", "token", "123456789"))
TEST_ACCOUNT_REFRESH_TOKEN = "-".join(("oauth", "refresh", "test", "token", "123456789"))
CURRENT_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "b" * 64
OUTDATED_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "a" * 64


class LocalLifecycleTests(LocalContractCase):
    def test_assistant_id_enumeration_avoids_deep_container_admission(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        controller.assistant_lifecycle._validate_container_security = mock.Mock(
            side_effect=AssertionError("deep admission must not run"),
        )

        self.assertEqual(controller.assistant_lifecycle._assistant_ids("team_1"), ("shimpz-cloudflare",))
        self.assertEqual(
            controller.assistant_lifecycle._assistant_ids("team_1", running_only=True), ("shimpz-cloudflare",)
        )
        self.assertEqual(events, [])
        controller.assistant_lifecycle._validate_container_security.assert_not_called()

    def test_assistant_lifecycle_is_rejected_before_mutation_during_an_active_chat(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        chat_lock = controller.chat_turn_service._chat_lock("team_1")
        self.assertTrue(chat_lock.acquire(blocking=False))
        try:
            operations = (
                controller.assistant_lifecycle.install_assistant,
                controller.assistant_lifecycle.uninstall_assistant,
            )
            for operation in operations:
                with self.subTest(operation=operation.__name__), self.assertRaises(local_app.ApiProblem) as caught:
                    operation("team_1", "shimpz-cloudflare")
                self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.CONFLICT, "chat-active"))
        finally:
            chat_lock.release()

        self.assertEqual(events, [])

    def test_install_replaces_an_outdated_release_after_current_contract_admission(self) -> None:
        controller, container, events = self._lifecycle_controller()
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda *_args: self.fail(
            "an outdated manifest must not block release replacement"
        )
        controller.assistant_integrations.put(
            "team_1",
            "shimpz-cloudflare",
            "undeclared-account",
            "cloudflare",
            ("zone.read",),
            SimpleNamespace(
                access_token=TEST_ACCOUNT_ACCESS_TOKEN,
                refresh_token=TEST_ACCOUNT_REFRESH_TOKEN,
                scopes=("zone.read",),
                expires_in=3600,
            ),
        )
        trusted_image = object()
        controller.assistant_lifecycle._trusted_image = lambda _spec: events.append("trusted") or trusted_image
        controller.assistant_lifecycle._create_assistant_container = lambda _team_id, _spec, _network, image: (
            events.append(("create", image))
        )

        result = controller.assistant_lifecycle.install_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "installed": False})
        self.assertEqual(events, ["reload", "trusted", "reload", ("remove", True), ("create", trusted_image)])
        self.assertEqual(container.attrs["Config"]["Image"], OUTDATED_ASSISTANT_IMAGE)
        self.assertFalse(controller.assistant_integrations.delete_assistant("team_1", "shimpz-cloudflare"))

    def test_update_commits_only_after_the_successor_is_running(self) -> None:
        controller, container, events = self._lifecycle_controller()
        base = controller.registry["shimpz-cloudflare"]
        previous = copy.copy(base)
        previous.image = OUTDATED_ASSISTANT_IMAGE
        successor = copy.copy(base)
        successor.integrations = {
            "cloudflare": SimpleNamespace(
                provider="cloudflare",
                scopes=("dns.read", "dns.write", "offline_access", "zone.read"),
            )
        }
        container.attrs["Image"] = "sha256:" + "a" * 64
        previous_image = SimpleNamespace(id="sha256:" + "a" * 64)
        successor_image = object()
        controller.client.images = SimpleNamespace(
            get=lambda image_id: events.append(("preserve", image_id)) or previous_image
        )
        controller.assistant_lifecycle._trusted_image = lambda spec: (
            events.append(("trust", spec.image)) or (successor_image)
        )
        controller.assistant_lifecycle._create_assistant_container = (
            lambda _team_id, spec, _network, image, **_options: events.append(("create", spec.image, image))
        )
        transaction = SimpleNamespace(previous_image_id=previous_image.id)
        controller.assistant_lifecycle.updates = SimpleNamespace(
            begin=lambda *_args: events.append("journal-begin") or transaction,
            clear=lambda update: events.append(("journal-clear", update)),
        )
        controller.assistant_lifecycle.residues = SimpleNamespace(
            add=lambda image_id: events.append(("residue-add", image_id))
        )
        controller.assistant_lifecycle.sweep_residues = lambda: events.append("residue-sweep")
        controller.chat_turn_service._retain_declared_assistant_integration_state = lambda _team_id, spec: (
            events.append(("retain", spec.image))
        )
        controller.registry = SimpleNamespace(commit_replacement=lambda *_args: events.append("commit"))
        controller.assistant_lifecycle.registry = controller.registry

        result = controller.assistant_lifecycle.update_assistant(
            "team_1",
            previous,
            successor,
            previous_binding=SimpleNamespace(binding_digest=f"sha256:{'1' * 64}"),
            resolution={},
            authorize_start=lambda: None,
        )

        self.assertEqual(
            result,
            {"assistant": "shimpz-cloudflare", "installed": False, "updated": True},
        )
        self.assertEqual(
            events,
            [
                "reload",
                ("trust", CURRENT_ASSISTANT_IMAGE),
                ("preserve", "sha256:" + "a" * 64),
                "journal-begin",
                ("remove", True),
                ("create", CURRENT_ASSISTANT_IMAGE, successor_image),
                "commit",
                ("retain", CURRENT_ASSISTANT_IMAGE),
                ("residue-add", previous_image.id),
                ("journal-clear", transaction),
                "residue-sweep",
            ],
        )

    def test_update_rejects_outbound_host_expansion_before_docker_mutation(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        previous = copy.copy(controller.registry["shimpz-cloudflare"])
        successor = copy.copy(previous)
        successor.allowed_hosts = ("api.cloudflare.com",)

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.update_assistant(
                "team_1",
                previous,
                successor,
                previous_binding=SimpleNamespace(binding_digest=f"sha256:{'1' * 64}"),
                resolution={},
                authorize_start=lambda: None,
            )

        self.assertEqual(caught.exception.code, "assistant-update-approval-required")
        self.assertEqual(events, [])

    def test_startup_recovers_the_binding_generation_and_clears_its_journal(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        previous_spec = copy.copy(controller.registry["shimpz-cloudflare"])
        successor_spec = copy.copy(previous_spec)
        successor_spec.image = OUTDATED_ASSISTANT_IMAGE
        previous_binding = object()
        successor_binding = object()
        update = SimpleNamespace(
            team_id="team_1",
            assistant_id="shimpz-cloudflare",
            previous=previous_binding,
            successor=successor_binding,
        )
        controller.registry = SimpleNamespace(
            binding=lambda *_args: previous_binding,
            spec=lambda binding: previous_spec if binding is previous_binding else successor_spec,
        )
        controller.assistant_lifecycle.registry = controller.registry
        controller.assistant_lifecycle._trusted_image = lambda spec: events.append(("trust", spec.image)) or object()
        controller.assistant_lifecycle._create_assistant_container = lambda _team_id, spec, _network, _image: (
            events.append(("create", spec.image))
        )
        controller.assistant_lifecycle.updates = SimpleNamespace(
            list=lambda: (update,),
            clear=lambda item: events.append(("journal-clear", item)),
        )
        controller.assistant_lifecycle.sweep_residues = lambda: events.append("residue-sweep")

        controller.assistant_lifecycle.recover_updates()

        self.assertEqual(
            events,
            [
                "reload",
                ("trust", CURRENT_ASSISTANT_IMAGE),
                ("remove", True),
                ("create", CURRENT_ASSISTANT_IMAGE),
                ("journal-clear", update),
                "residue-sweep",
            ],
        )

    def test_startup_isolates_an_unavailable_update_store_from_the_space(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        controller.assistant_lifecycle.updates = SimpleNamespace(
            list=lambda: (_ for _ in ()).throw(local_app.bindings.DynamicAssistantError("unavailable"))
        )

        controller.assistant_lifecycle.recover_updates()

    def test_retired_image_cleanup_is_exact_and_skips_shared_references(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        image_id = "sha256:" + "a" * 64
        controller.client.containers.list = lambda **_kwargs: []
        controller.client.images = SimpleNamespace(
            get=lambda _reference: SimpleNamespace(id="sha256:" + "b" * 64),
            remove=lambda **options: events.append(("image-remove", options)),
        )
        controller.registry = SimpleNamespace(all=lambda: ())
        controller.assistant_lifecycle.registry = controller.registry

        self.assertTrue(controller.assistant_lifecycle._remove_retired_image(image_id))
        self.assertEqual(
            events,
            [("image-remove", {"image": image_id, "force": False, "noprune": True})],
        )

        controller.registry = SimpleNamespace(all=lambda: (SimpleNamespace(image=OUTDATED_ASSISTANT_IMAGE),))
        controller.assistant_lifecycle.registry = controller.registry
        controller.client.images.get = lambda _reference: SimpleNamespace(id=image_id)
        self.assertFalse(controller.assistant_lifecycle._remove_retired_image(image_id))
        self.assertEqual(len(events), 1)

        controller.registry = SimpleNamespace(all=lambda: ())
        controller.assistant_lifecycle.registry = controller.registry
        controller.client.images.get = lambda _reference: SimpleNamespace(id="sha256:" + "b" * 64)
        controller.client.containers.list = lambda **_kwargs: [SimpleNamespace(attrs={"ImageID": image_id})]
        self.assertFalse(controller.assistant_lifecycle._remove_retired_image(image_id))
        self.assertEqual(len(events), 1)

    def test_residue_queue_failure_does_not_escape_the_completed_update(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        controller.assistant_lifecycle.residues = SimpleNamespace(
            add=lambda _image_id: (_ for _ in ()).throw(local_app.bindings.DynamicAssistantError("unavailable"))
        )
        controller.assistant_lifecycle._remove_retired_image = lambda _image_id: False

        controller.assistant_lifecycle._queue_residue("sha256:" + "a" * 64)

    def test_release_update_is_generic_for_future_assistants(self) -> None:
        controller, container, events = self._lifecycle_controller()
        spec = controller.registry.pop("shimpz-cloudflare")
        spec.assistant_id = "future-assistant"
        controller.registry[spec.assistant_id] = spec
        labels = container.attrs["Config"]["Labels"]
        labels[local_app.ASSISTANT_LABEL] = spec.assistant_id
        container.name = controller.assistant_lifecycle._container_name("team_1", spec.assistant_id)
        controller.assistant_lifecycle._trusted_image = lambda _spec: events.append("trusted") or object()
        controller.assistant_lifecycle._create_assistant_container = lambda *_args: events.append("create")
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda *_args: self.fail(
            "an outdated manifest must not block release discovery"
        )

        self.assertEqual(
            controller.list_assistants("team_1"),
            {"assistants": [{"assistant": "future-assistant", "assistant_version": "0.1.0", "status": "outdated"}]},
        )
        self.assertEqual(
            controller.assistant_lifecycle.install_assistant("team_1", "future-assistant"),
            {"assistant": "future-assistant", "installed": False},
        )
        self.assertEqual(events, ["reload", "reload", "trusted", "reload", ("remove", True), "create"])

    def test_listing_keeps_an_invalid_manifest_in_the_removable_inventory(self) -> None:
        controller, container, events = self._lifecycle_controller()
        container.attrs["Config"]["Image"] = CURRENT_ASSISTANT_IMAGE
        container.attrs["Config"]["Labels"][local_app.IMAGE_LABEL] = CURRENT_ASSISTANT_IMAGE
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant manifest does not match its reviewed contract",
                code="assistant-manifest-invalid",
            )
        )

        self.assertEqual(
            controller.list_assistants("team_1"),
            {"assistants": [{"assistant": "shimpz-cloudflare", "assistant_version": "0.1.0", "status": "invalid"}]},
        )
        self.assertEqual(events, ["reload"])
        controller.assistant_lifecycle._admit_assistant_allowed_hosts.assert_called_once_with(
            container,
            controller.registry["shimpz-cloudflare"],
        )

    def test_listing_propagates_manifest_transport_unavailability(self) -> None:
        controller, container, _events = self._lifecycle_controller()
        container.attrs["Config"]["Image"] = CURRENT_ASSISTANT_IMAGE
        container.attrs["Config"]["Labels"][local_app.IMAGE_LABEL] = CURRENT_ASSISTANT_IMAGE
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant manifest is unavailable",
                code="assistant-manifest-unavailable",
            )
        )

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.list_assistants("team_1")

        self.assertEqual(caught.exception.code, "assistant-manifest-unavailable")

    def test_manifest_admission_separates_transport_from_contract_failure(self) -> None:
        controller, container, _events = self._lifecycle_controller()
        spec = controller.registry["shimpz-cloudflare"]
        admit = local_app.AssistantLifecycle._admit_assistant_allowed_hosts.__get__(controller.assistant_lifecycle)
        failures = (
            (
                local_app.assistant_manifest.ManifestUnavailableError("Docker transport failed"),
                "assistant-manifest-unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
            ),
            (
                local_app.assistant_manifest.ManifestError("manifest contract failed"),
                "assistant-manifest-invalid",
                HTTPStatus.CONFLICT,
            ),
        )
        for failure, code, status in failures:
            with self.subTest(code=code):
                controller.assistant_lifecycle._assistant_allowed_hosts_cache = SimpleNamespace(
                    get=mock.Mock(side_effect=failure)
                )
                with self.assertRaises(local_app.ApiProblem) as caught:
                    admit(container, spec)

                self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def test_listing_keeps_egress_policy_drift_in_the_removable_inventory(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        controller.assistant_lifecycle._validate_container_egress = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant egress policy drifted",
                code="egress-policy-drift",
            )
        )
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = mock.Mock()

        self.assertEqual(
            controller.list_assistants("team_1"),
            {"assistants": [{"assistant": "shimpz-cloudflare", "assistant_version": "0.1.0", "status": "invalid"}]},
        )
        controller.assistant_lifecycle._admit_assistant_allowed_hosts.assert_not_called()

    def test_chat_stays_closed_for_a_manifest_invalid_inventory_entry(self) -> None:
        controller, container, _events = self._lifecycle_controller()
        container.attrs["Config"]["Image"] = CURRENT_ASSISTANT_IMAGE
        container.attrs["Config"]["Labels"][local_app.IMAGE_LABEL] = CURRENT_ASSISTANT_IMAGE
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant manifest is invalid",
                code="assistant-manifest-invalid",
            )
        )

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.chat_turn_service._active_chat_assistants(
                "team_1",
                controller.assistant_lifecycle._network_name("team_1"),
            )

        self.assertEqual(caught.exception.code, "assistant-manifest-invalid")

    def test_listing_fetches_the_egress_proxy_once_for_multiple_assistants(self) -> None:
        controller, first, _events = self._lifecycle_controller()
        first_spec = controller.registry["shimpz-cloudflare"]
        first_spec.allowed_hosts = ("api.example.com",)
        second_spec = copy.copy(first_spec)
        second_spec.assistant_id = "future-assistant"
        controller.registry[second_spec.assistant_id] = second_spec
        second = copy.deepcopy(first)
        second.labels[local_app.ASSISTANT_LABEL] = second_spec.assistant_id
        second.attrs["Config"]["Labels"][local_app.ASSISTANT_LABEL] = second_spec.assistant_id
        second.name = controller.assistant_lifecycle._container_name("team_1", second_spec.assistant_id)
        proxy_environment = {"HTTPS_PROXY": "http://shimpz-assistant-egress:8889"}
        for container in (first, second):
            container.attrs["Config"]["Env"] = [f"{key}={value}" for key, value in proxy_environment.items()]
        network_name = controller.assistant_lifecycle._network_name("team_1")
        proxy = SimpleNamespace(
            attrs={
                "NetworkSettings": {
                    "Networks": {
                        network_name: {
                            "Aliases": [ASSISTANT_EGRESS_ALIAS],
                        }
                    }
                }
            }
        )
        controller.client.containers.list = lambda **_kwargs: [first, second]
        controller.assistant_lifecycle._validate_egress_policy = lambda *_args: proxy_environment
        controller.assistant_lifecycle._egress_proxy = mock.Mock(return_value=proxy)

        result = controller.list_assistants("team_1")

        self.assertEqual(
            tuple(item["assistant"] for item in result["assistants"]),
            ("future-assistant", "shimpz-cloudflare"),
        )
        controller.assistant_lifecycle._egress_proxy.assert_called_once_with()

    def test_chat_inventory_uses_listed_attrs_and_one_egress_proxy_inspection(self) -> None:
        controller, first, events = self._lifecycle_controller()
        first_spec = controller.registry["shimpz-cloudflare"]
        first_spec.allowed_hosts = ("api.example.com",)
        second_spec = copy.copy(first_spec)
        second_spec.assistant_id = "future-assistant"
        controller.registry[second_spec.assistant_id] = second_spec
        second = copy.deepcopy(first)
        second.labels[local_app.ASSISTANT_LABEL] = second_spec.assistant_id
        second.attrs["Config"]["Labels"][local_app.ASSISTANT_LABEL] = second_spec.assistant_id
        second.name = controller.assistant_lifecycle._container_name("team_1", second_spec.assistant_id)
        proxy_environment = {"HTTPS_PROXY": "http://shimpz-assistant-egress:8889"}
        network_name = controller.assistant_lifecycle._network_name("team_1")
        for container in (first, second):
            container.attrs["Config"]["Image"] = CURRENT_ASSISTANT_IMAGE
            container.attrs["Config"]["Labels"][local_app.IMAGE_LABEL] = CURRENT_ASSISTANT_IMAGE
            container.attrs["Config"]["Env"] = [f"{key}={value}" for key, value in proxy_environment.items()]
        proxy = SimpleNamespace(
            attrs={
                "NetworkSettings": {
                    "Networks": {
                        network_name: {
                            "Aliases": [ASSISTANT_EGRESS_ALIAS],
                        }
                    }
                }
            }
        )
        controller.client.containers.list = mock.Mock(return_value=[first, second])
        controller.assistant_lifecycle._validate_egress_policy = lambda *_args: proxy_environment
        controller.assistant_lifecycle._egress_proxy = mock.Mock(return_value=proxy)

        active = controller.chat_turn_service._active_chat_assistants("team_1", network_name)

        self.assertEqual(tuple(item.spec.assistant_id for item in active), ("future-assistant", "shimpz-cloudflare"))
        self.assertEqual(events, [])
        controller.client.containers.list.assert_called_once_with(
            **controller.assistant_lifecycle._assistant_filters("team_1")
        )
        controller.assistant_lifecycle._egress_proxy.assert_called_once_with()

    def test_release_update_rejects_a_previous_security_contract(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        controller.registry["shimpz-cloudflare"].allowed_hosts = ("api.example.com",)
        controller.assistant_lifecycle._trusted_image = lambda _spec: self.fail(
            "contract drift reached image resolution"
        )

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.install_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(caught.exception.code, "egress-policy-drift")
        self.assertEqual(events, ["reload"])

    def test_container_profile_rejects_duplicate_or_malformed_environment_entries(self) -> None:
        invalid_environments = (
            ["SHIMPZ_TEAM_ID=team_1", "SHIMPZ_TEAM_ID=other"],
            ["HTTPS_PROXY=http://safe", "HTTPS_PROXY=http://evil"],
            ["missing-separator"],
        )
        for environment in invalid_environments:
            with self.subTest(environment=environment):
                controller, container, events = self._lifecycle_controller()
                container.attrs["Config"]["Env"] = environment

                with self.assertRaises(local_app.ApiProblem) as caught:
                    controller.list_assistants("team_1")

                self.assertEqual(caught.exception.code, "assistant-isolation-drift")
                self.assertEqual(events, ["reload"])

    def test_new_assistant_is_admitted_before_egress_and_start(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.cpuset_cpus = "0"
        spec = SimpleNamespace(
            assistant_id="shimpz-cloudflare",
            image=CURRENT_ASSISTANT_IMAGE,
            allowed_hosts=("api.open-meteo.com", "geocoding-api.open-meteo.com"),
        )
        image = SimpleNamespace(id="sha256:" + "d" * 64)
        container = SimpleNamespace(
            id="assistant-generation",
            attrs={"Image": image.id},
            reload=lambda: events.append("reload"),
            start=lambda: events.append("start"),
            remove=lambda *, force: events.append(("remove", force)),
        )
        controller.client = SimpleNamespace(
            containers=SimpleNamespace(
                create=lambda **_kwargs: events.append("create") or container,
            )
        )
        controller._wire_collaborators()
        network = SimpleNamespace(name=controller.assistant_lifecycle._network_name("team_1"))
        controller.assistant_lifecycle._egress_token = lambda *_args, **_kwargs: events.append("token") or "a" * 32
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda _container, _spec: (
            events.append("admit") or tuple(sorted(_spec.allowed_hosts))
        )
        controller.assistant_lifecycle._activate_assistant_egress = lambda *_args: events.append("activate-egress")
        controller.assistant_lifecycle._validate_container = lambda *_args: events.append("validate")
        controller.assistant_lifecycle._wait_ready = lambda *_args: events.append("ready")
        controller.assistant_lifecycle._active_assistant_genesis = lambda *_args: events.append("genesis") or "Genesis"

        controller.assistant_lifecycle._create_assistant_container("team_1", spec, network, image)

        self.assertLess(events.index("admit"), events.index("activate-egress"))
        self.assertLess(events.index("admit"), events.index("start"))
        self.assertEqual(events[-4:], ["start", "validate", "ready", "genesis"])

    def test_local_admission_reviews_hosts_and_integrations(self) -> None:
        controller = object.__new__(local_app.LocalController)
        controller._wire_collaborators()
        reviewed_contracts: list[local_app.assistant_manifest.ManifestContract] = []

        def admit(_container, reviewed):
            reviewed_contracts.append(reviewed)
            return reviewed

        controller.assistant_lifecycle._assistant_allowed_hosts_cache = SimpleNamespace(get=admit)
        controller.assistant_lifecycle._assistant_machine_contract_cache = SimpleNamespace(
            get=lambda _container, _integrations, reviewed: reviewed
        )
        spec = self._registry(CURRENT_ASSISTANT_IMAGE)["shimpz-cloudflare"]

        allowed_hosts = controller.assistant_lifecycle._admit_assistant_allowed_hosts(
            SimpleNamespace(id="generation"), spec
        )

        self.assertEqual(allowed_hosts, tuple(sorted(spec.allowed_hosts)))
        self.assertEqual(len(reviewed_contracts), 1)
        self.assertEqual(
            {account.id: (account.provider, account.scopes) for account in reviewed_contracts[0].integrations},
            {
                account_id: (account.provider, tuple(sorted(account.scopes)))
                for account_id, account in spec.integrations.items()
            },
        )
        exact = reviewed_contracts[0]
        account = exact.integrations[0]
        drifted = (
            replace(exact, integrations=(replace(account, provider="other"),)),
            replace(exact, integrations=(replace(account, scopes=("tweet.read",)),)),
        )
        controller.assistant_lifecycle._assistant_allowed_hosts_cache = (
            local_app.assistant_manifest.ManifestContractCache()
        )
        with mock.patch.object(
            local_app.assistant_manifest,
            "read_container_manifest_contract",
            return_value=exact,
        ):
            self.assertEqual(
                controller.assistant_lifecycle._admit_assistant_allowed_hosts(
                    SimpleNamespace(id="exact-generation"), spec
                ),
                exact.allowed_hosts,
            )
        for index, declared in enumerate(drifted):
            controller.assistant_lifecycle._assistant_allowed_hosts_cache = (
                local_app.assistant_manifest.ManifestContractCache()
            )
            with (
                self.subTest(declared=declared),
                mock.patch.object(
                    local_app.assistant_manifest,
                    "read_container_manifest_contract",
                    return_value=declared,
                ),
                self.assertRaises(local_app.ApiProblem) as drift,
            ):
                controller.assistant_lifecycle._admit_assistant_allowed_hosts(
                    SimpleNamespace(id=f"drift-generation-{index}"), spec
                )
            self.assertEqual(drift.exception.code, "assistant-manifest-invalid")
