"""Process-wide state and environment configuration for the hosted Team controller."""

from __future__ import annotations

import functools
import ipaddress
import math
import os
import threading
import time
import weakref
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

import docker

from action import challenges as action_challenges
from action import journal as action_journal
from assistant import genesis as assistant_genesis
from assistant import manifest as assistant_manifest
from hosted import container as container_spec
from hosted import token as token_store
from hosted.install import developers_client, developers_delegation
from inference import client as brain_runtime_client
from inference import config as inference_config
from install import artifact_trust, registry_auth
from install import bindings as dynamic_assistants
from install import icons as assistant_icons
from integrations import challenges as integration_challenges
from integrations import http as integration_http
from integrations import pkce as integration_pkce
from integrations import service as integration_service
from integrations import store as integration_store
from storage import files as team_storage

ALL_INTERFACES = str(ipaddress.IPv4Address(0))


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _validate_memory_budgets(global_bytes: int, owner_bytes: int, largest_resource: int) -> None:
    if global_bytes < largest_resource:
        raise ValueError("SHIMPZ_TEAM_GLOBAL_MEM_BUDGET is smaller than one Team resource")
    if not largest_resource <= owner_bytes <= global_bytes:
        raise ValueError("SHIMPZ_TEAM_OWNER_MEM_BUDGET must fit one resource and the global memory budget")


LISTEN_PORT = int(os.environ.get("SHIMPZ_TEAM_PORT", "7077"))
# The host has 125 GiB and each Team has a 2 GiB hard ceiling. The default leaves roughly half the
# host for the platform, installed Assistants, and Docker overhead; Supervisors may lower these quotas.
MAX_TEAMS = _positive_int_env("SHIMPZ_MAX_TEAMS", 32)
MAX_TEAMS_PER_OWNER = _positive_int_env("SHIMPZ_MAX_TEAMS_PER_OWNER", 1)
MAX_ASSISTANTS_PER_TEAM = _positive_int_env("SHIMPZ_MAX_ASSISTANTS_PER_TEAM", 20)
GLOBAL_MEMORY_BUDGET_BYTES = container_spec.hard_memory_bytes(
    os.environ.get("SHIMPZ_TEAM_GLOBAL_MEM_BUDGET", "64g"),
    setting="SHIMPZ_TEAM_GLOBAL_MEM_BUDGET",
)
OWNER_MEMORY_BUDGET_BYTES = container_spec.hard_memory_bytes(
    os.environ.get("SHIMPZ_TEAM_OWNER_MEM_BUDGET", "8g"),
    setting="SHIMPZ_TEAM_OWNER_MEM_BUDGET",
)
_LARGEST_RESOURCE_LIMIT = max(container_spec.MEM_LIMIT_BYTES, container_spec.ASSISTANT_MEM_LIMIT_BYTES)
_validate_memory_budgets(GLOBAL_MEMORY_BUDGET_BYTES, OWNER_MEMORY_BUDGET_BYTES, _LARGEST_RESOURCE_LIMIT)
MAX_JSON_BODY_BYTES = max(1024, int(os.environ.get("SHIMPZ_TEAM_MAX_JSON_BODY_BYTES", str(128 * 1024))))
MAX_TEAM_JSON_BODY_BYTES = 64 * 1024
CREATE_RATE_LIMIT = _positive_int_env("SHIMPZ_TEAM_CREATE_RATE_LIMIT", 5)
CREATE_RATE_WINDOW_SECONDS = _positive_int_env("SHIMPZ_TEAM_CREATE_RATE_WINDOW_SECONDS", 3600)
INSTALL_RATE_LIMIT = _positive_int_env("SHIMPZ_TEAM_INSTALL_RATE_LIMIT", 20)
INSTALL_RATE_WINDOW_SECONDS = _positive_int_env("SHIMPZ_TEAM_INSTALL_RATE_WINDOW_SECONDS", 3600)
CHAT_RATE_LIMIT = _positive_int_env("SHIMPZ_TEAM_CHAT_RATE_LIMIT", 30)
CHAT_RATE_WINDOW_SECONDS = _positive_int_env("SHIMPZ_TEAM_CHAT_RATE_WINDOW_SECONDS", 60)
FILE_UPLOAD_RATE_LIMIT = _positive_int_env("SHIMPZ_TEAM_FILE_UPLOAD_RATE_LIMIT", 60)
FILE_UPLOAD_RATE_WINDOW_SECONDS = _positive_int_env("SHIMPZ_TEAM_FILE_UPLOAD_RATE_WINDOW_SECONDS", 3600)
MAX_HTTP_CONCURRENCY = _positive_int_env("SHIMPZ_TEAM_MAX_HTTP_CONCURRENCY", 64)
HTTP_CONNECTION_TIMEOUT_SECONDS = _positive_int_env("SHIMPZ_TEAM_HTTP_CONNECTION_TIMEOUT_SECONDS", 30)
# One token-gated proxy serves every Assistant, with each token confined to its own allowlist in this volume.
ASSISTANT_EGRESS_POLICY_DIR = Path(os.environ.get("SHIMPZ_ASSISTANT_EGRESS_POLICY_DIR", "/assistant-egress-policy"))
ASSISTANT_EGRESS_POLICY_GID = 10017
TEAM_STORAGE_ROOT = Path("/var/lib/team/storage")
ACTION_JOURNAL_PATH = Path(
    os.environ.get(
        "SHIMPZ_TEAM_ACTION_JOURNAL_PATH",
        "/var/lib/team/action-journal/journal.sqlite3",
    )
)
ASSISTANT_INTEGRATION_STATE_PATH = Path(
    os.environ.get(
        "SHIMPZ_TEAM_ASSISTANT_INTEGRATION_STATE_PATH",
        "/var/lib/team/assistant-integrations/state/integrations.json",
    )
)
ASSISTANT_INTEGRATION_KEY_PATH = Path(
    os.environ.get(
        "SHIMPZ_TEAM_ASSISTANT_INTEGRATION_KEY_PATH",
        "/var/lib/team/assistant-integrations/key/aes256.key",
    )
)
DEVELOPERS_CONTROLLER_TOKEN_PATH = Path("/run/shimpz-developers-controller/developers-to-controller-token")
DEVELOPERS_DELEGATION_PUBLIC_KEY_PATH = Path("/run/shimpz-developers-controller/delegation-public.pem")
CONTROLLER_DEVELOPERS_TOKEN_PATH = Path("/run/shimpz-developers-controller/controller-to-developers-token")
REGISTRY_USERNAME_PATH = Path("/run/shimpz-developers-controller/assistant-registry-username")
REGISTRY_TOKEN_PATH = Path("/run/shimpz-developers-controller/assistant-registry-token")
DYNAMIC_ASSISTANT_PATH = Path(
    os.environ.get(
        "SHIMPZ_TEAM_DYNAMIC_ASSISTANT_PATH",
        "/var/lib/team/dynamic-assistants/bindings.json",
    )
)
COSIGN_TRUST_ROOT = Path(
    os.environ.get(
        "SHIMPZ_TEAM_COSIGN_TRUST_ROOT",
        "/var/lib/team/cosign",
    )
)
HEALTH_RETRIES = int(os.environ.get("SHIMPZ_HEALTH_RETRIES", "40"))
HEALTH_DELAY_SECONDS = float(os.environ.get("SHIMPZ_HEALTH_DELAY_SECONDS", "1.5"))

_docker = docker.from_env()
_registry_auth: registry_auth.RegistryAuth | None = None
_token = token_store.ensure_token()
# Weak maps retain one per-Team lock while a holder or waiter references it, without leaking entries
# after terminal operations or allowing an old locked object and a new unlocked object to coexist.
_locks_guard = threading.Lock()
_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_chat_locks_guard = threading.Lock()
_chat_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_active_chat_guard = threading.Lock()
_active_chat_tokens: dict[str, str] = {}
_active_chat_container_ids: dict[str, str] = {}
_active_action_container_ids: dict[str, tuple[str, str]] = {}
_blocked_action_workloads: set[tuple[str, str]] = set()
_cancelled_chat_tokens: set[str] = set()
# Docker inventory and slow provisioning run outside this lock. The generation detects snapshot churn.
_capacity_lock = threading.Lock()
_capacity_reservations: dict[str, object] = {}
_capacity_generation = 0
_storage_lock = threading.Lock()
_storage_instance: team_storage.TeamStorage | None = None
_action_journal_lock = threading.Lock()
_action_journal_instance: action_journal.ActionJournal | None = None
_brain_runtime = brain_runtime_client.BrainRuntimeClient()
_assistant_genesis_cache = assistant_genesis.GenesisCache()
_assistant_allowed_hosts_cache = assistant_manifest.ManifestContractCache()
_assistant_machine_contract_cache = assistant_manifest.MachineContractCache()
_assistant_integrations = integration_store.OAuthIntegrationStore(
    ASSISTANT_INTEGRATION_STATE_PATH,
    ASSISTANT_INTEGRATION_KEY_PATH,
)
_integration_challenges = integration_challenges.IntegrationChallengeStore()
_human_challenges = action_challenges.HumanChallengeStore()
_dynamic_assistants = dynamic_assistants.DynamicAssistantStore(DYNAMIC_ASSISTANT_PATH)
_assistant_icons = assistant_icons.AssistantIconStore(DYNAMIC_ASSISTANT_PATH.parent / "icons")
_integration_pkce = integration_pkce.OAuthPKCEChallengeStore()
_oauth_http = integration_http.OAuthHTTPClient()
_cloudflare_oauth_client_id = os.environ.get("SHIMPZ_CLOUDFLARE_OAUTH_CLIENT_ID")
_cloudflare_oauth_client_secret = os.environ.get("SHIMPZ_CLOUDFLARE_OAUTH_CLIENT_SECRET")
_oauth_integrations = integration_service.OAuthIntegrationService(
    client_id=_cloudflare_oauth_client_id,
    client_secret=_cloudflare_oauth_client_secret,
    redirect_uri=integration_http.HOSTED_REDIRECT_URI,
    challenge=_integration_pkce,
    store=_assistant_integrations,
    http=_oauth_http,
)
_inference_store = inference_config.InferenceConfigStore()
_developers_delegation: developers_delegation.DevelopersDelegationVerifier | None = None
_developers_client: developers_client.DevelopersClient | None = None
_artifact_trust: artifact_trust.ArtifactTrustVerifier | None = None


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _UnsupportedAssistantRpcPathError(RuntimeError):
    """The fixed Assistant RPC adapter rejected a path it does not implement."""


class _FixedWindowRateLimiter:
    """Thread-safe fixed-window admission with deterministic time injection for contract tests."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate limit and window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._guard = threading.Lock()
        self._counts: dict[str, tuple[int, int]] = {}
        self._last_bucket: int | None = None

    def consume(self, key: str, *, now: float | None = None) -> int:
        """Consume one event; return zero when allowed or whole retry-after seconds when denied."""
        current = time.monotonic() if now is None else now
        bucket = math.floor(current / self.window_seconds)
        with self._guard:
            if bucket != self._last_bucket:
                self._counts = {stored_key: value for stored_key, value in self._counts.items() if value[0] == bucket}
                self._last_bucket = bucket
            stored_bucket, count = self._counts.get(key, (bucket, 0))
            if stored_bucket != bucket:
                count = 0
            if count >= self.limit:
                boundary = (bucket + 1) * self.window_seconds
                return max(1, math.ceil(boundary - current))
            self._counts[key] = (bucket, count + 1)
        return 0


_rate_limiters = {
    "create": _FixedWindowRateLimiter(CREATE_RATE_LIMIT, CREATE_RATE_WINDOW_SECONDS),
    "install": _FixedWindowRateLimiter(INSTALL_RATE_LIMIT, INSTALL_RATE_WINDOW_SECONDS),
    "chat": _FixedWindowRateLimiter(CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_SECONDS),
    "stream": _FixedWindowRateLimiter(CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_SECONDS),
    "stop": _FixedWindowRateLimiter(CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_SECONDS),
    "file_upload": _FixedWindowRateLimiter(FILE_UPLOAD_RATE_LIMIT, FILE_UPLOAD_RATE_WINDOW_SECONDS),
}
_file_upload_slots = threading.BoundedSemaphore(2)


def _rate_key(principal: tuple[str, str | None]) -> str:
    kind, account_id = principal
    return f"{kind}:{account_id or 'absent'}"


def _enforce_rate(operation: str, principal: tuple[str, str | None]) -> None:
    retry_after = _rate_limiters[operation].consume(_rate_key(principal))
    if retry_after:
        raise ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            f"{operation} rate limit exceeded; retry in {retry_after}s",
        )


def _storage() -> team_storage.TeamStorage:
    global _storage_instance
    with _storage_lock:
        if _storage_instance is None:
            _storage_instance = team_storage.TeamStorage(TEAM_STORAGE_ROOT)
        return _storage_instance


def _action_execution_journal() -> action_journal.ActionJournal:
    """Open the private journal only when an Action batch or generation needs it."""
    global _action_journal_instance
    with _action_journal_lock:
        if _action_journal_instance is None:
            _action_journal_instance = action_journal.ActionJournal(ACTION_JOURNAL_PATH)
        return _action_journal_instance


def _initialize_developers_integration() -> None:
    """Load both directional service identities before the public server starts."""
    global _developers_delegation, _developers_client, _artifact_trust, _registry_auth
    _developers_delegation = developers_delegation.DevelopersDelegationVerifier(
        DEVELOPERS_CONTROLLER_TOKEN_PATH,
        DEVELOPERS_DELEGATION_PUBLIC_KEY_PATH,
    )
    _developers_client = developers_client.DevelopersClient(CONTROLLER_DEVELOPERS_TOKEN_PATH)
    _registry_auth = registry_auth.RegistryAuth.from_files(
        REGISTRY_USERNAME_PATH,
        REGISTRY_TOKEN_PATH,
    )
    _artifact_trust = artifact_trust.ArtifactTrustVerifier(
        _docker,
        credentials=_registry_auth,
        trust_root=COSIGN_TRUST_ROOT,
    )


def _lock_for(team_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(team_id)
        if lock is None:
            lock = threading.Lock()
            _locks[team_id] = lock
        return lock


def _chat_lock_for(team_id: str) -> threading.Lock:
    with _chat_locks_guard:
        lock = _chat_locks.get(team_id)
        if lock is None:
            lock = threading.Lock()
            _chat_locks[team_id] = lock
        return lock


def _serialize_against_team_chat(operation: Callable[..., dict]) -> Callable[..., dict]:
    """Reject lifecycle mutation before its first side effect while a Team turn owns the slot."""

    @functools.wraps(operation)
    def guarded(team_id: str, *args, **kwargs) -> dict:
        lock = _chat_lock_for(team_id)
        if not lock.acquire(blocking=False):
            raise ApiError(HTTPStatus.CONFLICT, "Team lifecycle cannot change during an active chat turn")
        try:
            return operation(team_id, *args, **kwargs)
        finally:
            lock.release()

    return guarded


def _clear_team_id_runtime_state(team_id: str) -> None:
    """Forget terminal in-memory state without deleting a lock that another request references."""
    with _active_chat_guard:
        token = _active_chat_tokens.pop(team_id, None)
        _active_chat_container_ids.pop(team_id, None)
        _active_action_container_ids.pop(team_id, None)
        for blocked in tuple(_blocked_action_workloads):
            if blocked[0] == team_id:
                _blocked_action_workloads.discard(blocked)
        if token is not None:
            _cancelled_chat_tokens.discard(token)


def _token_cancelled(token: str) -> bool:
    with _active_chat_guard:
        return token in _cancelled_chat_tokens


def _commit_chat_terminal(team_id: str, token: str) -> bool:
    """Linearization point: False means a user Stop acquired the token first."""
    with _active_chat_guard:
        if token in _cancelled_chat_tokens:
            return False
        if _active_chat_tokens.get(team_id) == token:
            _active_chat_tokens.pop(team_id, None)
            _active_chat_container_ids.pop(team_id, None)
        return True
