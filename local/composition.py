"""Explicit dependency inputs for the Local controller's composed services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssistantLifecycleDependencies:
    """External dependencies for Assistant lifecycle operations."""

    client: object | None = None
    space_id: str | None = None
    registry: object | None = None
    cpuset_cpus: str | None = None
    lock_for: object | None = None
    invoke: object | None = None
    list_assistants: object | None = None
    developers: object | None = None
    artifact_trust: object | None = None
    updates: object | None = None
    residues: object | None = None
    icons: object | None = None


@dataclass(frozen=True)
class ChatTurnDependencies:
    """External dependencies for local chat-turn operations."""

    space_id: str | None = None
    registry: object | None = None
    storage: object | None = None
    inference_store: object | None = None
    brain_runtime: object | None = None
    action_state: object | None = None
    assistant_integrations: object | None = None
    assistant_stored_inputs: object | None = None
    integration_challenges: object | None = None
    human_challenges: object | None = None
    oauth_pkce: object | None = None
    oauth_service: object | None = None
    chat_continuations: object | None = None
    lock_for: object | None = None
    raise_storage_problem: object | None = None
