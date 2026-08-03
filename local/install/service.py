"""Resolve, verify, and apply one immutable Local Assistant publication."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from install import artifact_trust, bindings
from local.errors import ApiProblemError as ApiProblem
from local.install import developers
from local.install.registry import is_successor
from local.validation import validate_team_id


def install_publication(
    self,
    team_id: str,
    assistant_id: str,
    source_digest: str,
    *,
    expected_binding_digest: str | None = None,
) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    existing = self.registry.binding(team_id, assistant_id)
    if expected_binding_digest is not None and (existing is None or existing.binding_digest != expected_binding_digest):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant binding changed before automatic update",
            code="assistant-update-conflict",
        )
    try:
        resolution = self.developers.resolve(source_digest)
        if resolution["assistant_id"] != assistant_id:
            raise developers.PublicationNotInstallableError("publication does not match the requested Assistant")
        self.artifact_trust.verify(resolution)

        def authorize_start() -> None:
            current = self.developers.resolve(source_digest)
            if current["assistant_id"] != assistant_id or current["oci_digest"] != resolution["oci_digest"]:
                raise developers.PublicationNotInstallableError("publication changed before installation")

        if existing is None:
            spec = self.registry.put(team_id, resolution)
            return self.assistant_lifecycle.install_assistant(
                team_id,
                spec.assistant_id,
                authorize_start=authorize_start,
            )
        return self._install_bound_publication(
            team_id,
            assistant_id,
            existing,
            resolution=resolution,
            authorize_start=authorize_start,
        )
    except ApiProblem as exc:
        if existing is None and exc.code != "assistant-install-rollback-incomplete":
            self.registry.delete(team_id, assistant_id)
        raise
    except developers.PublicationNotInstallableError as exc:
        if existing is None:
            self.registry.delete(team_id, assistant_id)
        raise ApiProblem(
            HTTPStatus.NOT_FOUND,
            "Assistant publication is not installable",
            code="assistant-not-installable",
        ) from exc
    except developers.DevelopersError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Developers is unavailable",
            code="developers-unavailable",
        ) from exc
    except artifact_trust.ArtifactTrustError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant artifact trust failed",
            code="assistant-artifact-untrusted",
        ) from exc
    except bindings.DynamicAssistantError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant publication binding failed",
            code="assistant-binding-conflict",
        ) from exc


def _install_bound_publication(
    self,
    team_id: str,
    assistant_id: str,
    existing: bindings.DynamicAssistantBinding,
    *,
    resolution: dict[str, object],
    authorize_start: Callable[[], None],
) -> dict[str, object]:
    candidate, successor = self.registry.replacement(
        team_id,
        existing.binding_digest,
        resolution,
    )
    if candidate == existing:
        return self.assistant_lifecycle.install_assistant(
            team_id,
            successor.assistant_id,
            authorize_start=authorize_start,
        )
    if not is_successor(existing, candidate):
        raise developers.PublicationNotInstallableError("publication is not a newer Assistant version")
    previous = self.registry.get(team_id, assistant_id)
    if previous is None:
        raise bindings.DynamicAssistantConflictError("the Assistant binding changed before update")
    return self.assistant_lifecycle.update_assistant(
        team_id,
        previous,
        successor,
        previous_binding=existing,
        resolution=resolution,
        authorize_start=authorize_start,
    )
