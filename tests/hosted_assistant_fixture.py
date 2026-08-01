"""Import-isolated Hosted Team fixture shared by Assistant contract suites."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
importlib.import_module("hosted")
importlib.import_module("hosted.team")

_MODULES_BEFORE_APP_LOAD = dict(sys.modules)


class _DockerError(Exception):
    pass


class _NotFoundError(_DockerError):
    pass


class _APIError(_DockerError):
    pass


class _Passthru:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _LogConfig(_Passthru):
    types = types.SimpleNamespace(JSON="json-file")


class _EmptyCollection:
    @staticmethod
    def get(_identity):
        raise _NotFoundError

    @staticmethod
    def list(**_kwargs):
        return []


_engine = types.SimpleNamespace(
    containers=_EmptyCollection(),
    networks=_EmptyCollection(),
    volumes=_EmptyCollection(),
    images=_EmptyCollection(),
)
_docker_types = types.ModuleType("docker.types")
_docker_types.Mount = _Passthru
_docker_types.Ulimit = _Passthru
_docker_types.Healthcheck = _Passthru
_docker_types.LogConfig = _LogConfig
_docker_errors = types.ModuleType("docker.errors")
_docker_errors.DockerException = _DockerError
_docker_errors.NotFound = _NotFoundError
_docker_errors.APIError = _APIError
_docker_errors.ImageNotFound = _NotFoundError
_docker_socket = types.ModuleType("docker.utils.socket")
_docker_utils = types.ModuleType("docker.utils")
_docker_utils.socket = _docker_socket
_docker = types.ModuleType("docker")
_docker.from_env = lambda: _engine
_docker.types = _docker_types
_docker.errors = _docker_errors
_docker.utils = _docker_utils
sys.modules.update(
    {
        "docker": _docker,
        "docker.types": _docker_types,
        "docker.errors": _docker_errors,
        "docker.utils": _docker_utils,
        "docker.utils.socket": _docker_socket,
    }
)


def _stub(name: str, **members):
    module = types.ModuleType(name)
    for key, value in members.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent_name, separator, child_name = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if separator and parent is not None:
        setattr(parent, child_name, module)
    return module


class _IntegrationSecretError(Exception):
    pass


class _IntegrationSecretSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass


class _PostgreSQLServiceError(Exception):
    pass


class _AuthorityDeniedError(RuntimeError):
    pass


class _AuthorityUnavailableError(RuntimeError):
    pass


def _authority_session_token(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or not value.isascii()
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
    ):
        raise _AuthorityDeniedError
    return value


class _AuthorityEvaluation:
    def __init__(self, account_id, supervisor, binding_digest, owner_account_id) -> None:
        self.account_id = account_id
        self.supervisor = supervisor
        self.binding_digest = binding_digest
        self.owner_account_id = owner_account_id

    @property
    def principal(self):
        return ("supervisor" if self.supervisor else "account", self.account_id)


_stub(
    "hosted.authority",
    EMPTY_SHA256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    AuthorityDeniedError=_AuthorityDeniedError,
    AuthorityUnavailableError=_AuthorityUnavailableError,
    Evaluation=_AuthorityEvaluation,
    session_token=_authority_session_token,
    binding_digest=lambda binding: hashlib.sha256(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest(),
    evaluate=lambda *_args: None,
)
_stub("hosted.audit", log=lambda *_args, **_kwargs: "trace")
_stub(
    "inference.integration_secrets",
    IntegrationSecretError=_IntegrationSecretError,
    IntegrationSecretSession=_IntegrationSecretSession,
    resolve=lambda *_args: None,
    generation_is_current=lambda *_args: True,
)
_stub(
    "hosted.team.postgresql",
    PostgreSQLServiceError=_PostgreSQLServiceError,
    provision_team=lambda _team_id: {"database_url": "postgres://scoped"},
    drop_team=lambda *_args: {},
    finalize_team_drop=lambda *_args: {},
)
_stub("hosted.token", ensure_token=lambda: "team-machine-token")

spec = importlib.util.spec_from_file_location("team_assistant_hosted_test", TEAM / "hosted" / "app.py")
app = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = app
spec.loader.exec_module(app)

runtime_state = sys.modules["hosted.state"]
hosted_resources = sys.modules["hosted.team.resources"]
assistant_lifecycle = sys.modules["hosted.assistant.lifecycle"]
hosted_lifecycle = sys.modules["hosted.team.lifecycle"]
hosted_assistants = sys.modules["hosted.assistant.runtime"]
hosted_chat_api = sys.modules["hosted.chat.api"]
hosted_chat_segment = sys.modules["hosted.chat.segment"]
hosted_controller = sys.modules["hosted.http.server"]
hosted_developers_http = sys.modules["hosted.install.http"]

from local_assistant_fixture import hosted_spec as _hosted_spec

HOSTED_SPEC = _hosted_spec("ghcr.io/theshimpz/shimpz-assistant@sha256:" + ("a" * 64))
HOSTED_BINDING = types.SimpleNamespace(
    team_id="team_1",
    assistant_id="shimpz-cloudflare",
    binding_digest="sha256:" + ("b" * 64),
    resolution={
        "assistant_id": "shimpz-cloudflare",
        "source_digest": "sha256:" + ("c" * 64),
        "oci_digest": "sha256:" + ("a" * 64),
    },
)


class _BindingStore:
    def get(self, team_id, assistant_id):
        if (team_id, assistant_id) == ("team_1", "shimpz-cloudflare"):
            return HOSTED_BINDING
        return None

    def list(self, team_id):
        return (HOSTED_BINDING,) if team_id == "team_1" else ()

    def put(self, _team_id, resolution):
        return types.SimpleNamespace(
            team_id="team_1",
            assistant_id=resolution["assistant_id"],
            binding_digest="sha256:" + ("b" * 64),
            resolution=resolution,
        )

    @staticmethod
    def delete(_team_id, _assistant_id):
        return True


runtime_state._dynamic_assistants = _BindingStore()

# The loaded app keeps direct references to its fakes. Restore the process import table so discovery
# order can never make unrelated tests import a partial Docker/client module.
for module_name, module in tuple(sys.modules.items()):
    source = getattr(module, "__file__", None)
    if source is None:
        continue
    try:
        belongs_to_team = Path(source).resolve().is_relative_to(TEAM)
    except OSError, RuntimeError, ValueError:
        belongs_to_team = False
    if belongs_to_team and module_name not in {__name__, spec.name}:
        previous = _MODULES_BEFORE_APP_LOAD.get(module_name)
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        parent_name, separator, child_name = module_name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if separator and parent is not None:
            if previous is None and getattr(parent, child_name, None) is module:
                delattr(parent, child_name)
            elif previous is not None:
                setattr(parent, child_name, previous)
for module_name in (
    "docker",
    "docker.types",
    "docker.errors",
    "docker.utils",
    "docker.utils.socket",
    "hosted.authority",
    "hosted.audit",
    "inference.integration_secrets",
    "hosted.team.postgresql",
    "hosted.token",
):
    current = sys.modules.get(module_name)
    previous = _MODULES_BEFORE_APP_LOAD.get(module_name)
    if previous is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = previous
    parent_name, separator, child_name = module_name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if separator and parent is not None:
        if previous is None and getattr(parent, child_name, None) is current:
            delattr(parent, child_name)
        elif previous is not None:
            setattr(parent, child_name, previous)

ANCHOR_ID = "a" * 64
