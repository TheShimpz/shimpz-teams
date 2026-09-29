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
KNOWLEDGE_SCHEMA = 1
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
# Every name this store writes: a Team's configuration, its learned knowledge, and their interrupted temp files.
OWNED_NAME_RE = re.compile(
    r"(?:[0-9a-f]{64}(?:\.knowledge)?\.json|\.[0-9a-f]{64}(?:\.knowledge)?\.json\.[0-9a-f]{16}\.tmp)\Z"
)


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

    def _knowledge_path(self, team_id: str) -> Path:
        digest = hashlib.sha256(team_id.encode()).hexdigest()
        return self.root / f"{digest}.knowledge.json"

    def save(self, team_id: object, config: InferenceConfig) -> InferenceConfig:
        team_id = _team_id(team_id)
        validated = normalize(config.provider, config.model, config.effort)
        self._write(self._path(team_id), {"schema": SCHEMA, "team_id": team_id, **asdict(validated)})
        return validated

    def load_knowledge(self, team_id: object) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
        """The Team's learned memory and skills (ADR-0084, ADR-0085); an absent file means nothing is learned yet."""
        team_id = _team_id(team_id)
        try:
            value = json.loads(self._knowledge_path(team_id).read_bytes())
        except FileNotFoundError:
            return [], []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InferenceConfigError("Team knowledge is unavailable") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "team_id", "memory", "skills"}
            or value["schema"] != KNOWLEDGE_SCHEMA
            or value["team_id"] != team_id
        ):
            raise InferenceConfigError("Team knowledge is invalid")
        memory = http_payload.canonical_memory(value["memory"])
        skills = http_payload.canonical_skills(value["skills"])
        if memory is None or skills is None or not (memory or skills):
            raise InferenceConfigError("Team knowledge is invalid")
        return memory, skills

    def apply_knowledge(
        self, team_id: object, changes: object, skill: object
    ) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
        """Apply one committed turn's memory changes and new skill in one atomic write; nothing learned, no file."""
        team_id = _team_id(team_id)
        admitted = http_payload.canonical_memory_changes(changes)
        learned = None if skill is None else http_payload.canonical_skill(skill)
        if admitted is None or (skill is not None and learned is None):
            raise InferenceConfigError("Team knowledge changes are invalid")
        memory, skills = http_payload.apply_knowledge(*self.load_knowledge(team_id), admitted, learned)
        target = self._knowledge_path(team_id)
        if not (memory or skills):
            self._unlink(target)
            return memory, skills
        record = {"schema": KNOWLEDGE_SCHEMA, "team_id": team_id, "memory": memory, "skills": skills}
        try:
            self._write(target, record)
        except OSError as exc:
            raise InferenceConfigError("Team knowledge could not be saved") from exc
        return memory, skills

    def _write(self, target: Path, value: dict[str, object]) -> None:
        self._prepare()
        temporary = self.root / f".{target.name}.{secrets.token_hex(8)}.tmp"
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                # The mode is final before the swap, so a failure can never leave a readable replaced file.
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
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
        """Remove the Team's inference configuration and its learned knowledge."""
        team_id = _team_id(team_id)
        self._unlink(self._path(team_id))
        self._unlink(self._knowledge_path(team_id))

    def delete_all(self) -> None:
        """Remove every Team's configuration and knowledge, including ones no Team network names now."""
        try:
            owned = [path for path in self.root.iterdir() if OWNED_NAME_RE.fullmatch(path.name)]
        except FileNotFoundError:
            return
        except OSError as exc:
            raise InferenceConfigError("Team inference configuration could not be listed") from exc
        for path in owned:
            self._unlink(path)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise InferenceConfigError("Team inference configuration could not be removed") from exc
