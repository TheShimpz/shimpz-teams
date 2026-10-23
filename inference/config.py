"""Small Team-owned provider/model registry that never stores secrets."""

import hashlib
import json
import re
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from core import identifier
from inference import memory as inference_memory
from protocol.http.v1 import payload as http_payload
from storage import private_state

SCHEMA = 3
KNOWLEDGE_SCHEMA = 2
MAX_CONFIG_BYTES = 4096
# Well above 32 memories and their provenance at their field bounds.
MAX_KNOWLEDGE_BYTES = 1024 * 1024


_MODEL_CATALOG = json.loads(Path(__file__).with_name("model_catalog.json").read_text(encoding="utf-8"))
# Each catalog provider's id and the exact model ids a person may choose for a Team's chat; a non-chat catalog model is
# one Brain runs itself (the Browser's), never a Team's configuration.
PROVIDERS: dict[str, frozenset[str]] = {
    provider["id"]: frozenset(model["id"] for model in provider["models"] if model["chat"] is True)
    for provider in _MODEL_CATALOG["providers"]
}
# Each catalog model's input and output price in cents per million tokens, by provider and model id.
PRICES: dict[str, dict[str, tuple[int, int]]] = {
    provider["id"]: {
        model["id"]: (model["input_usd_per_million_cents"], model["output_usd_per_million_cents"])
        for model in provider["models"]
    }
    for provider in _MODEL_CATALOG["providers"]
}


def price(provider: str, model: str | None) -> tuple[int, int]:
    """A provider model's catalog price; an unknown or unnamed model is priced as the provider's dearest."""
    prices = PRICES[provider]
    if model in prices:
        return prices[model]
    return max(prices.values(), key=lambda pair: pair[0] + pair[1])


# Every name this store writes: a Team's configuration, its learned knowledge, and their interrupted temp files.
OWNED_NAME_RE = re.compile(
    r"(?:[0-9a-f]{64}(?:\.knowledge)?\.json|\.[0-9a-f]{64}(?:\.knowledge)?\.json\.[0-9a-f]{16}\.tmp)\Z"
)


class InferenceConfigError(ValueError):
    """Inference metadata is invalid or its private store failed closed."""


class InferenceConfigMissingError(InferenceConfigError):
    """The Team has no inference configuration yet."""


@dataclass(frozen=True, slots=True)
class InferenceConfig:
    provider: str
    model: str


def normalize(provider: object, model: object) -> InferenceConfig:
    """Admit exactly one catalog provider and one of its catalog models; nothing is defaulted or coerced."""
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise InferenceConfigError(f"provider must be one of {sorted(PROVIDERS)}")
    if not isinstance(model, str) or model not in PROVIDERS[provider]:
        raise InferenceConfigError("model is not supported by the selected provider")
    return InferenceConfig(provider=provider, model=model)


def _team_id(value: object) -> str:
    return identifier.require(http_payload.canonical_team_id, value, InferenceConfigError, "invalid Team id")


class InferenceConfigStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, team_id: str, suffix: str = ".json") -> Path:
        """A Team's record, named by its id's SHA-256 so no id ever becomes a path."""
        return self.root / f"{hashlib.sha256(team_id.encode()).hexdigest()}{suffix}"

    def _knowledge_path(self, team_id: str) -> Path:
        return self._path(team_id, ".knowledge.json")

    def save(self, team_id: object, config: InferenceConfig) -> InferenceConfig:
        team_id = _team_id(team_id)
        validated = normalize(config.provider, config.model)
        self._write(self._path(team_id), {"schema": SCHEMA, "team_id": team_id, **asdict(validated)}, MAX_CONFIG_BYTES)
        return validated

    def load_knowledge(self, team_id: object) -> list[dict[str, str]]:
        """The Team's learned memory as a turn carries it: topic and preference, never provenance (ADR-0121)."""
        return inference_memory.projection(self._memory(_team_id(team_id)))

    def _memory(self, team_id: str) -> list[dict[str, object]]:
        """The Team's private memory entries (ADR-0084); an absent file means nothing is learned yet."""
        try:
            value = private_state.read_json_record(self._knowledge_path(team_id), MAX_KNOWLEDGE_BYTES)
        except (OSError, ValueError) as exc:
            raise InferenceConfigError("Team knowledge is unavailable") from exc
        if value is None:
            return []
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "team_id", "memory"}
            or value["schema"] != KNOWLEDGE_SCHEMA
            or value["team_id"] != team_id
        ):
            raise InferenceConfigError("Team knowledge is invalid")
        memory = inference_memory.canonical_entries(value["memory"])
        if not memory:
            raise InferenceConfigError("Team knowledge is invalid")
        return memory

    def plan_knowledge(self, team_id: object, changes: object, source: object) -> inference_memory.Plan:
        """What one committed turn's memory changes leave, stamped with the turn's source; nothing is written yet."""
        team_id = _team_id(team_id)
        admitted = http_payload.canonical_memory_changes(changes)
        captured = inference_memory.canonical_source(source)
        if admitted is None or captured is None:
            raise InferenceConfigError("Team knowledge changes are invalid")
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return inference_memory.plan(self._memory(team_id), admitted, captured, since)

    def save_knowledge(self, team_id: object, plan: inference_memory.Plan) -> None:
        """Keep a planned memory in one atomic write; nothing learned, no file."""
        team_id = _team_id(team_id)
        target = self._knowledge_path(team_id)
        if not plan.memory:
            self._unlink(target)
            return
        record = {"schema": KNOWLEDGE_SCHEMA, "team_id": team_id, "memory": list(plan.memory)}
        try:
            self._write(target, record, MAX_KNOWLEDGE_BYTES)
        except (OSError, ValueError) as exc:
            raise InferenceConfigError("Team knowledge could not be saved") from exc

    def _write(self, target: Path, value: dict[str, object], maximum: int) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        private_state.write_json_record(target, value, maximum)

    def load(self, team_id: object) -> InferenceConfig:
        team_id = _team_id(team_id)
        try:
            value = private_state.read_json_record(self._path(team_id), MAX_CONFIG_BYTES)
        except OSError as exc:
            raise InferenceConfigError("Team inference configuration is unavailable") from exc
        except ValueError as exc:
            raise InferenceConfigError("Team inference configuration is invalid") from exc
        if value is None:
            raise InferenceConfigMissingError("Team inference configuration is not set")
        if not isinstance(value, dict) or set(value) != {"schema", "team_id", "provider", "model"}:
            raise InferenceConfigError("Team inference configuration is invalid")
        if value["schema"] != SCHEMA or value["team_id"] != team_id:
            raise InferenceConfigError("Team inference configuration is invalid")
        return normalize(value["provider"], value["model"])

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
        """Durably remove one owned record: a returned removal survives power loss; an absent one already succeeded.

        The parent directory is fsynced even when the record was already absent, so a retry after a failed sync
        still commits the earlier removal; an absent store has nothing to commit.
        """
        try:
            path.unlink(missing_ok=True)
            with suppress(FileNotFoundError):
                private_state.fsync_directory(path.parent)
        except OSError as exc:
            raise InferenceConfigError("Team inference configuration could not be removed") from exc
