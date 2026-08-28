"""Resolve, verify, and apply one immutable Local Assistant publication."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from http import HTTPStatus

from install import artifact_trust, bindings, icons
from local.errors import ApiProblemError as ApiProblem
from local.install import developers, snapshots
from local.install.registry import is_successor
from local.validation import validate_team_id


def list_local_snapshots(self) -> dict[str, object]:
    try:
        candidates = snapshots.list_candidates(self.client)
    except snapshots.LocalSnapshotUnavailableError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant snapshots are unavailable",
            code="local-assistant-snapshots-unavailable",
        ) from exc
    except snapshots.LocalSnapshotError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Local Assistant snapshot inventory is invalid",
            code="local-assistant-snapshots-invalid",
        ) from exc
    return {
        "assistants": [
            {
                "assistant_id": candidate.assistant_id,
                "assistant_version": candidate.version,
                "image_id": candidate.image_id,
                "platform": candidate.platform,
                "created_at": candidate.created_at,
                "provenance": "local",
                "unpublished": True,
            }
            for candidate in candidates
        ]
    }


def install_local_snapshot(self, team_id: str, image_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    admitted = _admit_local_snapshot(self, image_id)
    assistant_id = admitted.record["assistant_id"]
    existing = self.registry.binding(team_id, assistant_id)
    if existing is not None and existing.provenance != "local":
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Uninstall the published Assistant before installing a Local snapshot",
            code="assistant-provenance-conflict",
        )
    candidate = bindings.binding_from_local_record(team_id, admitted.record, snapshots.validate_record)
    try:
        self.assistant_icons.put_local(admitted.record, admitted.icon)
        result = _apply_local_snapshot(self, team_id, existing, admitted.record)
    except ApiProblem as exc:
        if existing is None and exc.code != "assistant-install-rollback-incomplete":
            self.registry.delete(team_id, assistant_id)
        _discard_local_icon(self, candidate)
        raise
    except bindings.DynamicAssistantError as exc:
        if existing is None:
            self.registry.delete(team_id, assistant_id)
        _discard_local_icon(self, candidate)
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Local Assistant binding failed",
            code="assistant-binding-conflict",
        ) from exc
    except icons.AssistantIconError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant icon storage is unavailable",
            code="assistant-icon-unavailable",
        ) from exc
    if existing is not None:
        _discard_local_icon(self, existing)
    return {
        **result,
        "provenance": "local",
        "image_id": image_id,
        "unpublished": True,
    }


def _admit_local_snapshot(self, image_id: str) -> snapshots.AdmittedLocalSnapshot:
    try:
        return snapshots.admit(self.client, image_id)
    except snapshots.LocalSnapshotUnavailableError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant snapshot is unavailable",
            code="local-assistant-snapshot-unavailable",
        ) from exc
    except snapshots.LocalSnapshotError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Local Assistant snapshot failed admission",
            code="local-assistant-snapshot-invalid",
        ) from exc


def _apply_local_snapshot(
    self,
    team_id: str,
    existing: bindings.DynamicAssistantBinding | None,
    record: dict[str, object],
) -> dict[str, object]:
    assistant_id = str(record["assistant_id"])
    if existing is None:
        spec = self.registry.put_local(team_id, record)
        return self.assistant_lifecycle.install_assistant(team_id, spec.assistant_id)
    candidate, successor = self.registry.local_replacement(
        team_id,
        existing.binding_digest,
        record,
    )
    if candidate == existing:
        return self.assistant_lifecycle.install_assistant(team_id, assistant_id)
    if existing.local_record["image_id"] == record["image_id"]:
        raise bindings.DynamicAssistantConflictError("the Local Assistant replacement image id is unchanged")
    previous = self.registry.get(team_id, assistant_id)
    if previous is None:
        raise bindings.DynamicAssistantConflictError("the Assistant binding changed before update")
    return self.assistant_lifecycle.update_assistant(
        team_id,
        previous,
        successor,
        previous_binding=existing,
        successor_document=record,
        authorize_start=lambda: None,
    )


def _discard_local_icon(self, binding: bindings.DynamicAssistantBinding) -> None:
    try:
        self.assistant_icons.discard_binding(binding, self.registry.bindings())
    except icons.AssistantIconError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant icon storage is unavailable",
            code="assistant-icon-unavailable",
        ) from exc


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
    publication_resolved = False
    installation_completed = False
    try:
        resolution = _resolved_publication(self, assistant_id, source_digest)
        publication_resolved = True
        result = _apply_publication(self, team_id, assistant_id, source_digest, existing, resolution)
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
    except icons.AssistantIconError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant icon storage is unavailable",
            code="assistant-icon-unavailable",
        ) from exc
    else:
        if existing is not None:
            _discard_icon(self, str(existing.resolution["source_digest"]))
        installation_completed = True
        return result
    finally:
        _discard_failed_publication(
            self,
            source_digest,
            publication_resolved=publication_resolved,
            installation_completed=installation_completed,
        )


def _discard_failed_publication(
    self,
    source_digest: str,
    *,
    publication_resolved: bool,
    installation_completed: bool,
) -> None:
    if publication_resolved and not installation_completed:
        _discard_icon(self, source_digest)


def _resolved_publication(self, assistant_id: str, source_digest: str) -> dict[str, object]:
    resolution = self.developers.resolve(source_digest)
    if resolution["assistant_id"] != assistant_id:
        raise developers.PublicationNotInstallableError("publication does not match the requested Assistant")
    icon = _verify_publication_assets(self, source_digest, resolution)
    self.assistant_icons.put(resolution, icon)
    return resolution


def _verify_publication_assets(self, source_digest: str, resolution: dict[str, object]) -> bytes:
    icon_context = copy_context()
    trust_context = copy_context()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="assistant-publication") as executor:
        icon_future = executor.submit(
            icon_context.run,
            self.developers.icon,
            source_digest,
            resolution["icon_digest"],
        )
        trust_future = executor.submit(trust_context.run, self.artifact_trust.verify, resolution)
    trust_error = trust_future.exception()
    icon_error = icon_future.exception()
    if trust_error is not None:
        raise trust_error
    if icon_error is not None:
        raise icon_error
    return icon_future.result()


def _apply_publication(self, team_id, assistant_id, source_digest, existing, resolution):
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


def _discard_icon(self, source_digest: str) -> None:
    try:
        self.assistant_icons.discard_unreferenced(source_digest, self.registry.bindings())
    except icons.AssistantIconError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant icon storage is unavailable",
            code="assistant-icon-unavailable",
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
        successor_document=resolution,
        authorize_start=authorize_start,
    )
