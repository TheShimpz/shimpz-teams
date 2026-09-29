"""Small Team-owned provider/model registry that never stores secrets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict

from protocol.http.v1 import payload as http_payload

ROOT = Path(os.environ.get("SHIMPZ_TEAM_INFERENCE_DIR", "/var/lib/team/inference"))
SCHEMA = 2
INSTRUCTIONS_SCHEMA = 1
# The reasoning effort a Team's ordinary chat turns use; a new configuration starts at the default (ADR-0074).
EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORT = "low"


class ProviderDefinition(TypedDict):
    title: str
    default_model: str
    models: frozenset[str]


_MODEL_CATALOG = json.loads(Path(__file__).with_name("model_catalog.json").read_text(encoding="utf-8"))
PROVIDERS: dict[str, ProviderDefinition] = {
    provider["id"]: {
        "title": provider["title"],
        "default_model": provider["default_model"],
        "models": frozenset(model["id"] for model in provider["models"]),
    }
    for provider in _MODEL_CATALOG["providers"]
}
DEFAULT_PROVIDER = _MODEL_CATALOG["default_provider"]
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
TEAM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class InferenceConfigError(ValueError):
    """Inference metadata is invalid or its private store failed closed."""


class InferenceConfigMissingError(InferenceConfigError):
    """The Team has no inference configuration yet; only this state may be initialized with defaults."""


@dataclass(frozen=True, slots=True)
class InferenceConfig:
    provider: str
    model: str
    effort: str = DEFAULT_EFFORT


def normalize(provider: object = None, model: object = None, effort: object = None) -> InferenceConfig:
    selected = str(provider or DEFAULT_PROVIDER).strip().lower()
    if selected not in PROVIDERS:
        raise InferenceConfigError(f"provider must be one of {sorted(PROVIDERS)}")
    selected_model = str(model or PROVIDERS[selected]["default_model"]).strip()
    if MODEL_RE.fullmatch(selected_model) is None or selected_model not in PROVIDERS[selected]["models"]:
        raise InferenceConfigError("model is not supported by the selected provider")
    selected_effort = DEFAULT_EFFORT if effort is None else effort
    if selected_effort not in EFFORTS:
        raise InferenceConfigError(f"effort must be one of {list(EFFORTS)}")
    return InferenceConfig(provider=selected, model=selected_model, effort=selected_effort)


def _team_id(value: object) -> str:
    team_id = str(value or "")
    if TEAM_ID_RE.fullmatch(team_id) is None:
        raise InferenceConfigError("invalid Team id")
    return team_id


class InferenceConfigStore:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def _path(self, team_id: str) -> Path:
        digest = hashlib.sha256(team_id.encode()).hexdigest()
        return self.root / f"{digest}.json"

    def _instructions_path(self, team_id: str) -> Path:
        digest = hashlib.sha256(team_id.encode()).hexdigest()
        return self.root / f"{digest}.instructions.json"

    def save(self, team_id: object, config: InferenceConfig) -> InferenceConfig:
        team_id = _team_id(team_id)
        validated = normalize(config.provider, config.model, config.effort)
        self._write(self._path(team_id), {"schema": SCHEMA, "team_id": team_id, **asdict(validated)})
        return validated

    def save_instructions(self, team_id: object, instructions: object) -> list[str]:
        """Replace the Team's standing instructions (ADR-0083); an empty list removes them."""
        team_id = _team_id(team_id)
        rules = http_payload.canonical_instructions(instructions)
        if rules is None:
            raise InferenceConfigError("standing instructions are invalid")
        if not rules:
            self._unlink(self._instructions_path(team_id))
            return []
        self._write(
            self._instructions_path(team_id),
            {"schema": INSTRUCTIONS_SCHEMA, "team_id": team_id, "instructions": rules},
        )
        return rules

    def load_instructions(self, team_id: object) -> list[str]:
        team_id = _team_id(team_id)
        try:
            value = json.loads(self._instructions_path(team_id).read_bytes())
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InferenceConfigError("Team standing instructions are unavailable") from exc
        rules = (
            http_payload.canonical_instructions(value["instructions"])
            if isinstance(value, dict)
            and set(value) == {"schema", "team_id", "instructions"}
            and value["schema"] == INSTRUCTIONS_SCHEMA
            and value["team_id"] == team_id
            else None
        )
        if not rules:
            raise InferenceConfigError("Team standing instructions are invalid")
        return rules

    def _write(self, target: Path, value: dict[str, object]) -> None:
        self._prepare()
        temporary = self.root / f".{target.name}.{secrets.token_hex(8)}.tmp"
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
            target.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, team_id: object) -> InferenceConfig:
        team_id = _team_id(team_id)
        try:
            raw = self._path(team_id).read_bytes()
        except FileNotFoundError as exc:
            raise InferenceConfigMissingError("Team inference configuration is not set") from exc
        except OSError as exc:
            raise InferenceConfigError("Team inference configuration is unavailable") from exc
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InferenceConfigError("Team inference configuration is invalid") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "team_id", "provider", "model", "effort"}:
            raise InferenceConfigError("Team inference configuration is invalid")
        if value["schema"] != SCHEMA or value["team_id"] != team_id or not isinstance(value["effort"], str):
            raise InferenceConfigError("Team inference configuration is invalid")
        return normalize(value["provider"], value["model"], value["effort"])

    def delete(self, team_id: object) -> None:
        """Remove the Team's inference configuration and its standing instructions."""
        team_id = _team_id(team_id)
        self._unlink(self._path(team_id))
        self._unlink(self._instructions_path(team_id))

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise InferenceConfigError("Team inference configuration could not be removed") from exc
