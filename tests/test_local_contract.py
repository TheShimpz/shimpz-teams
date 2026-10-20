import json
import os
import sys
import tempfile
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
import browser_fixtures
from local_controller_harness import (
    CURRENT_ASSISTANT_IMAGE,
    LOOKUP_INPUT,
    LOOKUP_RESULT,
    TEST_ACCOUNT_ACCESS_TOKEN,
    LocalContractCase,
    chat_body,
    constructed_controller,
    wire_collaborators,
)

from assistant import spec as assistant_registry
from inference import config as inference_config
from inference import token as brain_runtime_token_store
from local import app as local_app
from local import audit as local_audit
from local import healthcheck as local_healthcheck
from local import names as local_names
from local import validation as local_validation
from local.assistant import egress as local_egress
from local.assistant import lifecycle as assistant_lifecycle
from local.chat import continuation_store as local_chat_continuation_store
from local.chat import service as local_chat_service
from local.http import chat as http_chat
from local.http import server as local_http
from local.http import token as local_token_store
from local.routine import diagnostics as local_routine_diagnostics
from local.routine import store as local_routine_store
from local.validation import validate_model_credential_headers


class LocalContractTests(LocalContractCase):
    def test_local_state_defaults_match_the_installer_mount_contract(self) -> None:
        self.assertEqual(local_token_store.TOKEN_PATH, Path("/run/shimpz-local/token"))
        self.assertEqual(local_healthcheck.TOKEN_PATH, local_token_store.TOKEN_PATH)
        self.assertEqual(local_audit.AUDIT_PATH, Path("/var/log/shimpz-local/audit.jsonl"))
        self.assertEqual(local_app.STORAGE_ROOT, Path("/var/lib/shimpz-local/storage"))
        self.assertEqual(local_app.INFERENCE_ROOT, Path("/var/lib/shimpz-local/inference"))
        self.assertEqual(
            local_app.LOCAL_ACTION_JOURNAL_PATH,
            Path("/var/lib/shimpz-local/action-journal/journal.sqlite3"),
        )
        self.assertEqual(
            local_chat_continuation_store.STATE_PATH,
            Path("/var/lib/shimpz-local/chat-continuations/state/continuations.json"),
        )
        self.assertEqual(
            local_chat_continuation_store.KEY_PATH,
            Path("/var/lib/shimpz-local/chat-continuations/key/aes256.key"),
        )
        self.assertEqual(local_egress.ASSISTANT_EGRESS_POLICY_DIR, Path("/var/lib/shimpz-local/assistant-egress"))
        self.assertEqual(brain_runtime_token_store.TOKEN_PATH, Path("/run/shimpz-brain-runtime/token"))

    def test_routine_diagnostics_stay_inside_the_mounted_routine_volumes(self) -> None:
        # The Local graph mounts exactly these two Routine volumes; Team's root filesystem is read-only.
        self.assertEqual(local_routine_store.ROOT, Path("/var/lib/shimpz-local/routines/state"))
        self.assertEqual(local_routine_store.KEY_PATH.parent, Path("/var/lib/shimpz-local/routines/key"))
        self.assertEqual(local_routine_diagnostics.ROOT, local_routine_store.ROOT / "diagnostics")
        self.assertEqual(local_routine_diagnostics.KEY_PATH, local_routine_store.KEY_PATH.with_name("diagnostics.key"))

    def test_cloudflare_assistant_contract_is_read_only_closed_and_bounded(self) -> None:
        registry = self._registry(CURRENT_ASSISTANT_IMAGE)
        spec = registry["shimpz-cloudflare"]
        self.assertEqual(spec.allowed_hosts, ("api.cloudflare.com",))
        self.assertEqual(set(spec.actions), {"list-zones", "list-dns-records"})
        self.assertTrue(all(action.integrations == ("cloudflare",) for action in spec.actions.values()))
        self.assertEqual(
            assistant_registry.validate_action_payload(
                spec.actions["list-dns-records"],
                "input",
                {"zone_id": "a" * 32, "page": 1, "per_page": 100},
            ),
            {"zone_id": "a" * 32, "page": 1, "per_page": 100},
        )
        zones = {
            "zones": [
                {
                    "id": "a" * 32,
                    "name": "example.com",
                    "status": "active",
                    "type": "full",
                    "paused": False,
                    "account": {"id": "b" * 32, "name": "Shimpz"},
                }
            ],
            "pagination": {"page": 1, "per_page": 100, "count": 1, "total_count": 1, "total_pages": 1},
        }
        self.assertEqual(
            assistant_registry.validate_action_payload(spec.actions["list-zones"], "output", zones),
            zones,
        )
        with self.assertRaises(ValueError):
            assistant_registry.validate_action_payload(
                spec.actions["list-zones"],
                "output",
                zones | {"access_token": "must-not-cross"},
            )
        for invalid in (
            {"page": 0, "per_page": 100},
            {"page": 1, "per_page": 101},
            {"zone_id": "../zone", "page": 1, "per_page": 10},
            {"page": 1, "per_page": 10, "access_token": "must-not-cross"},
        ):
            action = "list-dns-records" if "zone_id" in invalid else "list-zones"
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                assistant_registry.validate_action_payload(spec.actions[action], "input", invalid)

    def test_identifiers_are_strict_and_bounded(self) -> None:
        self.assertEqual(local_app.validate_team_id("demo_team"), "demo_team")
        for invalid in ("Demo", "-demo", "demo-1", "a" * 41, "demo space", ""):
            with self.subTest(invalid=invalid), self.assertRaises(local_app.ApiProblem) as caught:
                local_app.validate_team_id(invalid)
            self.assertEqual(caught.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_local_thread_identity_is_scoped_to_the_network_generation(self) -> None:
        first = local_validation.brain_thread_id("local-space", "team_1", "a" * 64)
        second = local_validation.brain_thread_id("local-space", "team_1", "b" * 64)

        self.assertEqual(first, f"local:local-space:team_1:{'a' * 64}:default")
        self.assertNotEqual(first, second)
        for space_id, team_id, network_id in (
            ("bad space", "team_1", "a" * 64),
            ("local-space", "bad-team", "a" * 64),
            ("local-space", "team_1", "not-a-docker-id"),
        ):
            with (
                self.subTest(
                    space_id=space_id,
                    team_id=team_id,
                    network_id=network_id,
                ),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                local_validation.brain_thread_id(space_id, team_id, network_id)
            self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_team_name_matches_the_admin_contract(self) -> None:
        self.assertEqual(local_names.canonical_name("My Team"), "My Team")
        self.assertEqual(local_names.canonical_name("\u00c9quipe"), "\u00c9quipe")
        # A decomposed name is refused, so a Local display name always compares exactly (ADR-0088).
        invalid_names = ("", " padded", "padded ", "x\n", "x" * 81, None)
        invalid_names += ("E\u0301quipe", "Growth\u200b", "Growth\ud800")
        for invalid in invalid_names:
            with self.subTest(invalid=invalid), self.assertRaises(local_app.ApiProblem):
                local_names.canonical_name(invalid)

    def test_container_limits_and_stateless_recovery_are_intentionally_narrow(self) -> None:
        self.assertEqual(assistant_lifecycle.ASSISTANT_NANO_CPUS, 250_000_000)
        self.assertEqual(assistant_lifecycle.ASSISTANT_MEMORY, 128 * 1024 * 1024)
        self.assertEqual(assistant_lifecycle.ASSISTANT_PIDS, 64)
        self.assertEqual(local_app.half_cpu_set(96), "0-47")
        self.assertEqual(local_app.half_cpu_set(8), "0-3")
        self.assertEqual(local_app.half_cpu_set(1), "0")
        readiness = local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "not ready", code="assistant-not-ready")
        ownership = local_app.ApiProblem(HTTPStatus.CONFLICT, "drift", code="ownership-conflict")
        self.assertTrue(assistant_lifecycle._is_replaceable_readiness_failure(readiness))
        self.assertFalse(assistant_lifecycle._is_replaceable_readiness_failure(ownership))

    def test_local_controller_bootstraps_tokens_before_serving(self) -> None:
        events: list[str] = []
        client = SimpleNamespace(close=lambda: events.append("client-close"))
        server = SimpleNamespace(
            serve_forever=lambda **_kwargs: events.append("serve"),
            server_close=lambda: events.append("server-close"),
            activity=object(),
        )
        updater = SimpleNamespace(
            start=lambda: events.append("updates-start"),
            close=lambda: events.append("updates-close"),
        )

        with (
            mock.patch.dict(os.environ, {"SHIMPZ_SPACE_ID": "local-space"}),
            mock.patch.object(
                local_app.local_token_store,
                "issue_token",
                side_effect=lambda: events.append("controller-token") or "a" * 64,
            ),
            mock.patch.object(
                local_app.brain_runtime_token_store,
                "ensure",
                side_effect=lambda: events.append("runtime-token") or "b" * 64,
            ),
            mock.patch.object(
                local_app.docker,
                "DockerClient",
                side_effect=lambda **_kwargs: events.append("docker") or client,
            ),
            mock.patch.object(
                local_app.team_storage,
                "TeamStorage",
                side_effect=lambda _path: events.append("storage") or SimpleNamespace(),
            ),
            mock.patch.object(
                local_app,
                "AssistantRegistry",
                side_effect=lambda _store: events.append("registry") or SimpleNamespace(),
            ),
            mock.patch.object(
                local_app.artifact_trust,
                "ArtifactTrustVerifier",
                return_value=SimpleNamespace(),
            ),
            mock.patch.object(
                local_app,
                "LocalController",
                side_effect=lambda *_args: (
                    events.append("controller")
                    or SimpleNamespace(
                        local_snapshot_inventory=SimpleNamespace(
                            warm=lambda: events.append("snapshots-warm"),
                        ),
                        chat_turn_service=mock.sentinel.chat,
                        browser_service=SimpleNamespace(startup=lambda: events.append("browser-startup")),
                        sign_in_store=SimpleNamespace(reconcile=lambda: events.append("sign-ins-reconciled")),
                    )
                ),
            ),
            mock.patch.object(local_app.local_browser_runtime, "own_browser_image", return_value=None),
            # The Browser cleanup runs on its own thread; here it runs at once, so the order is exact.
            mock.patch.object(
                local_app.threading, "Thread", side_effect=lambda target, **_options: SimpleNamespace(start=target)
            ),
            mock.patch.object(
                local_app,
                "BoundedServer",
                side_effect=lambda *_args: events.append("server") or server,
            ),
            mock.patch.object(
                local_app.local_automatic_updates,
                "AutomaticAssistantUpdater",
                side_effect=lambda *_args, **_options: events.append("updates") or updater,
            ),
            mock.patch.object(local_app.local_audit, "record", side_effect=lambda *_args, **_kwargs: "trace"),
            mock.patch.object(
                local_app.local_routine_watchdog,
                "check",
                side_effect=lambda service, *, startup: events.append(("routines-recover", service, startup)),
            ),
            mock.patch.object(
                local_app.local_routine_watchdog,
                "RoutineWatchdog",
                return_value=SimpleNamespace(
                    start=lambda: events.append("routines-start"), close=lambda: events.append("routines-close")
                ),
            ),
        ):
            result = local_app.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                "controller-token",
                "runtime-token",
                "docker",
                "registry",
                "storage",
                "controller",
                "server",
                "snapshots-warm",
                "updates",
                "sign-ins-reconciled",
                "browser-startup",
                ("routines-recover", mock.sentinel.chat, True),
                "updates-start",
                "routines-start",
                "serve",
                "routines-close",
                "updates-close",
                "server-close",
                "client-close",
            ],
        )

    def test_automatic_update_audit_uses_a_redacted_machine_principal(self) -> None:
        with mock.patch.object(local_app.local_audit, "record") as record:
            local_app._record_automatic_update(
                "team_1",
                "hello-world",
                "ok",
                "updated:0.1.0:0.2.0",
            )

        record.assert_called_once_with(
            "assistant-update",
            result="ok",
            principal=local_app.local_audit.AuditPrincipal("team-local", "machine"),
            team_id="team_1",
            assistant="hello-world",
            detail="updated:0.1.0:0.2.0",
        )

    def test_local_controller_accepts_an_injected_action_journal(self) -> None:
        injected = SimpleNamespace()
        controller = constructed_controller(action_state=injected)

        self.assertIs(controller.action_state, injected)
        self.assertIsInstance(controller.assistant_lifecycle, local_app.AssistantLifecycle)
        self.assertIsInstance(controller.chat_turn_service, local_app.ChatTurnService)
        self.assertEqual(local_app.LocalController.__bases__, (object,))
        self.assertEqual(local_app.AssistantLifecycle.__bases__, (object,))
        self.assertEqual(local_app.ChatTurnService.__bases__, (object,))
        self.assertFalse(hasattr(local_app.LocalController, "__getattr__"))
        self.assertIsNot(controller.assistant_lifecycle.__dict__, controller.__dict__)
        self.assertIsNot(controller.chat_turn_service.__dict__, controller.__dict__)
        self.assertIsNot(controller.assistant_lifecycle.__dict__, controller.chat_turn_service.__dict__)
        self.assertIs(controller.assistant_lifecycle.chat_turn_service, controller.chat_turn_service)
        self.assertIs(controller.chat_turn_service.assistant_lifecycle, controller.assistant_lifecycle)
        # Team destroy and Space reset drop the Routine books chat opens and answers on: they are the same objects.
        books = ("routine_human_challenges", "routine_recordings", "routine_proposals")
        for book in (*books, "routine_protections"):
            with self.subTest(book=book):
                self.assertIs(getattr(controller, book), getattr(controller.chat_turn_service, book))
        explicit_registry = object()
        explicit_storage = object()
        standalone_lifecycle = local_app.AssistantLifecycle(
            local_app.AssistantLifecycleDependencies(registry=explicit_registry)
        )
        standalone_chat = local_app.ChatTurnService(local_app.ChatTurnDependencies(storage=explicit_storage))
        self.assertIs(standalone_lifecycle.registry, explicit_registry)
        self.assertIs(standalone_chat.storage, explicit_storage)
        for module in (
            local_app.local_assistant_lifecycle,
            local_app.local_assistant_resources,
            local_app.local_assistant_rpc,
            local_chat_service.local_chat_api,
            local_app.local_chat_execution,
            local_chat_service.local_chat_pause,
            local_chat_service.local_chat_private,
            local_chat_service.local_chat_resume,
            local_chat_service.local_chat_segment,
            local_app.local_chat_state,
            local_app.local_egress,
            local_app.local_team_lifecycle,
        ):
            self.assertFalse(any(name.endswith("Mixin") for name in vars(module)))
        self.assertEqual(
            local_app.LOCAL_ACTION_JOURNAL_PATH,
            Path("/var/lib/shimpz-local/action-journal/journal.sqlite3"),
        )

    def test_ambiguous_action_rpc_is_fail_stopped_or_permanently_blocked(self) -> None:
        controller = object.__new__(local_app.LocalController)
        browser_fixtures.attach(controller)
        wire_collaborators(controller)

        class Stoppable:
            id = "stoppable"
            status = "running"

            def __init__(self) -> None:
                self.attrs = {"State": {"Running": True}}

            def stop(self, *, timeout: int) -> None:
                self.status = "exited"
                self.attrs["State"]["Running"] = False

            def reload(self) -> None:
                return None

            def kill(self) -> None:
                raise AssertionError("a proved stop must not be killed")

        stopped = Stoppable()
        controller.assistant_lifecycle._fail_stop_action(stopped)
        self.assertEqual(stopped.status, "exited")
        self.assertNotIn(stopped.id, controller.assistant_lifecycle._blocked_action_workloads)

        class Paused:
            id = "paused"
            status = "paused"
            killed = False

            def __init__(self) -> None:
                self.attrs = {"State": {"Running": True}}

            def stop(self, *, timeout: int) -> None:
                return None

            def reload(self) -> None:
                return None

            def kill(self) -> None:
                self.killed = True
                self.attrs["State"]["Running"] = False

        paused = Paused()
        controller.assistant_lifecycle._fail_stop_action(paused)
        self.assertTrue(paused.killed)
        self.assertNotIn(paused.id, controller.assistant_lifecycle._blocked_action_workloads)

    def test_unprovable_action_stop_is_permanently_blocked(self) -> None:
        controller = object.__new__(local_app.LocalController)
        browser_fixtures.attach(controller)
        wire_collaborators(controller)

        class Ambiguous:
            id = "ambiguous"

            def stop(self, *, timeout: int) -> None:
                raise local_app.DockerException("ambiguous stop")

            def reload(self) -> None:
                raise local_app.DockerException("ambiguous inspect")

            def kill(self) -> None:
                raise local_app.DockerException("ambiguous kill")

        ambiguous = Ambiguous()
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle._fail_stop_action(ambiguous)
        self.assertEqual(caught.exception.code, "assistant-action-blocked")
        self.assertIn(ambiguous.id, controller.assistant_lifecycle._blocked_action_workloads)

        class Malformed:
            id = "malformed"

            def __init__(self) -> None:
                self.attrs = {"State": {}}

            def stop(self, *, timeout: int) -> None:
                return None

            def reload(self) -> None:
                return None

            def kill(self) -> None:
                return None

        malformed = Malformed()
        with self.assertRaises(local_app.ApiProblem):
            controller.assistant_lifecycle._fail_stop_action(malformed)
        self.assertIn(malformed.id, controller.assistant_lifecycle._blocked_action_workloads)

    def test_large_upload_admission_is_single_slot(self) -> None:
        self.assertTrue(local_http._FILE_UPLOAD_SLOTS.acquire(blocking=False))
        try:
            self.assertFalse(local_http._FILE_UPLOAD_SLOTS.acquire(blocking=False))
        finally:
            local_http._FILE_UPLOAD_SLOTS.release()

    def test_inference_configuration_persists_only_provider_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            browser_fixtures.attach(controller)
            controller._locks = tuple(__import__("threading").RLock() for _ in range(64))
            controller.inference_store = inference_config.InferenceConfigStore(Path(directory) / "inference")
            wire_collaborators(controller)
            controller.assistant_lifecycle._network = lambda _team_id: object()

            configured = controller.configure_inference(
                "team_1",
                {"provider": "openai", "model": "gpt-6-luna"},
            )

            self.assertEqual(
                configured,
                {"team_id": "team_1", "provider": "openai", "model": "gpt-6-luna"},
            )
            self.assertEqual(controller.inference_status("team_1"), configured)
            stored = next((Path(directory) / "inference").iterdir()).read_text(encoding="utf-8")
            self.assertNotIn("api_key", stored)
            for body in (
                {"provider": "openai"},
                {"provider": "openai", "model": "gpt-6-luna", "api_key": "never"},
            ):
                with self.subTest(body=body), self.assertRaises(local_app.ApiProblem):
                    controller.configure_inference("team_1", body)

    def test_private_model_headers_are_closed_and_never_echoed(self) -> None:
        key = "sk-test-0123456789"
        self.assertEqual(
            validate_model_credential_headers(["openai"], [key]),
            ("openai", key),
        )
        invalid = (
            ([], [key]),
            (["openai", "openai"], [key]),
            (["unsupported"], [key]),
            (["openai"], []),
            (["openai"], [key, key]),
            (["openai"], ["short"]),
            (["openai"], ["x" * 16 + "\n"]),
        )
        for providers, keys in invalid:
            with self.subTest(providers=providers, keys=len(keys)), self.assertRaises(local_app.ApiProblem) as caught:
                validate_model_credential_headers(providers, keys)
            self.assertNotIn(key, str(caught.exception))

    def test_private_chat_route_reads_key_from_header_not_json(self) -> None:
        key = "sk-test-0123456789"
        payload = chat_body("Hello", assistant_ids=["shimpz-cloudflare"])
        body = json.dumps(payload).encode()
        captured: dict[str, object] = {}

        class Controller:
            chat_turn_service = SimpleNamespace(
                chat=lambda team_id, payload, provider, api_key, _progress=None: (
                    captured.update(
                        team_id=team_id,
                        payload=payload,
                        provider=provider,
                        api_key=api_key,
                    )
                    or {"team_name": "Marketing", "reply": "Hello!"}
                )
            )

        token_value = "a" * 32
        handler = object.__new__(local_app.Handler)
        handler.command = "POST"
        handler.server = SimpleNamespace(controller=Controller(), token=token_value)
        handler.headers = Message()
        handler.headers["Authorization"] = f"Bearer {token_value}"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.headers["X-Shimpz-Model-Provider"] = "openai"
        handler.headers["X-Shimpz-Model-Api-Key"] = key
        handler.rfile = BytesIO(body)

        self.assertTrue(handler._authorized())
        handler.capture_body("chat")
        status, response = http_chat._start(handler, "team_1", None)

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(captured["payload"], payload)
        self.assertEqual(captured["provider"], "openai")
        self.assertEqual(captured["api_key"], key)
        self.assertNotIn(key, json.dumps(response))

    def test_action_rpc_receives_only_the_spec_v1_invocation(self) -> None:
        captured: list[object] = []
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, object())

            brokers: list[object] = []

            def rpc(_container, action_id, payload, broker):
                captured.append((action_id, payload))
                brokers.append(broker)
                return {"type": "result", "result": LOOKUP_RESULT}

            controller.assistant_lifecycle._rpc = rpc
            audit = mock.patch.object(local_app.local_audit, "record_request")
            audit.start()
            self.addCleanup(audit.stop)
            with mock.patch.object(local_app.local_audit, "record_request"):
                response = controller.invoke("team_1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        self.assertTrue(local_app.action_journal.valid_operation_id(captured[0][1].pop("operation_id")))
        self.assertEqual(
            captured,
            [
                (
                    "list-zones",
                    {
                        "input": LOOKUP_INPUT,
                        "stored_inputs": (),
                        "files": {},
                    },
                )
            ],
        )
        self.assertEqual(response["result"], LOOKUP_RESULT)
        # The Integration token stays with Team's broker, placed only on its provider's API host (ADR-0106).
        self.assertEqual(
            [(item.host, item.value) for item in brokers[0]._scope.credentials],
            [("api.cloudflare.com", f"Bearer {TEST_ACCOUNT_ACCESS_TOKEN}")],
        )

    def test_action_output_containing_a_secret_is_blocked_and_redacted(self) -> None:
        raw_secret = TEST_ACCOUNT_ACCESS_TOKEN
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, object())
            controller.assistant_lifecycle._rpc = lambda *_args: {
                "type": "result",
                "result": {
                    "id": "123456789",
                    "name": f"unsafe {raw_secret}",
                    "username": "OpenAI",
                },
            }
            with (
                mock.patch.object(local_app.local_audit, "record_request"),
                self.assertRaises(local_app.ApiProblem) as leaked,
            ):
                controller.invoke("team_1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        self.assertEqual(leaked.exception.code, "assistant-secret-exposure")
        self.assertNotIn(raw_secret, str(leaked.exception))
