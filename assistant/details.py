"""One Assistant page in one interface language, projected from an admitted binding or staged snapshot."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import payload as http_payload


class StoredInputCopy(Protocol):
    """A declared Stored Input's English label and help text, and its help link."""

    @property
    def label(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def help_url(self) -> str: ...


@dataclass(frozen=True, slots=True)
class AssistantPage:
    """The admitted identity, displayed English copy, and declared capabilities an Assistant page shows."""

    assistant_id: str
    version: str
    name: str
    # Self-declared handles: a published resolution's creators or a staged snapshot's first four, never authority.
    creators: tuple[str, ...]
    summary: str
    description: str
    links: Mapping[str, str]
    machine_contract: Mapping[str, Any]
    # Integration id to its provider, and Stored Input id to its declared copy.
    integrations: Mapping[str, str]
    stored_inputs: Mapping[str, StoredInputCopy]

    def localized(self, locale: str, pack: assistant_language.LanguagePack | None) -> dict[str, object]:
        """The closed details object in one interface language; English needs no pack, any other its admitted one.

        Every displayed text is one catalog message, so a translation is that message's template from the pack.
        Raises ManifestError when the projection is not the exact protocol object.
        """
        text = _english if locale == assistant_language.ENGLISH else _translation(locale, pack)
        details = {
            "locale": locale,
            "assistant_id": self.assistant_id,
            "assistant_version": self.version,
            "name": self.name,
            "creators": list(self.creators),
            "summary": text(self.summary),
            "description": text(self.description),
            "links": dict(self.links),
            "actions": [
                {"id": action["id"], "effect": action["effect"], "description": text(action["description"])}
                for action in self.machine_contract["actions"]
            ],
            "integrations": [
                {"id": identifier, "provider": provider} for identifier, provider in sorted(self.integrations.items())
            ],
            "stored_inputs": [
                {
                    "id": identifier,
                    "label": text(stored_input.label),
                    "description": text(stored_input.description),
                    "help_url": stored_input.help_url,
                }
                for identifier, stored_input in sorted(self.stored_inputs.items())
            ],
        }
        if http_payload.canonical_assistant_details(details) is None:
            raise assistant_manifest.ManifestError("Assistant details are invalid")
        return details


def _english(text: str) -> str:
    return text


def _translation(locale: str, pack: assistant_language.LanguagePack | None) -> Callable[[str], str]:
    if pack is None:
        raise assistant_manifest.ManifestError("Assistant details need the binding's language pack")
    return lambda text: pack.template(catalog_validator.message_id(text), locale)
