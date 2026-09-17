"""Resolve, verify, and apply one immutable Local Assistant publication."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from http import HTTPStatus

from install import artifact_trust, bindings, icons
from local.errors import ApiProblemError as ApiProblem
from local.install import developers, snapshots
from local.install.registry import is_successor
from local.validation import validate_team_id

_LOCAL_PREVIEW_SLOTS = threading.BoundedSemaphore(2)


def list_local_snapshots(self) -> dict[str, object]:
    try:
        candidates = snapshots.list_candidates(self.client)
    except snapshots.LocalSnapshotUnavailableError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant snapshots are unavailable",
            code="local-assistant-snapshots-unavailable",
        ) from exc
    except snapshots.InvalidLabeledSnapshotError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            str(exc),
            code="local-assistant-snapshots-invalid",
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
                "name": candidate.name,
                "summary": candidate.summary,
                "declared_creators": list(candidate.declared_creators),
                "actions": list(candidate.actions),
                "integrations": list(candidate.integrations),
                "image_id": candidate.image_id,
                "platform": candidate.platform,
                "created_at": candidate.created_at,
                "provenance": "local",
                "unpublished": True,
            }
            for candidate in candidates
        ]
    }


def local_snapshot_icon(self, image_id: str) -> bytes:
    if not _LOCAL_PREVIEW_SLOTS.acquire(blocking=False):
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant preview capacity is busy",
            code="local-assistant-preview-busy",
        )
    try:
        return snapshots.preview_icon(self.client, image_id)
    except snapshots.LocalSnapshotUnavailableError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant preview is unavailable",
            code="local-assistant-preview-unavailable",
        ) from exc
    except snapshots.LocalSnapshotError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Local Assistant preview failed admission",
            code="local-assistant-preview-invalid",
        ) from exc
    finally:
        _LOCAL_PREVIEW_SLOTS.release()


def install_local_snapshot(self, team_id: str, image_id: str) -> dict[str, object]:
    return _install_local_snapshot(self, team_id, image_id, fresh_only=False)


def install_fresh_local_snapshot(self, team_id: str, image_id: str) -> dict[str, object]:
    """Install one exact snapshot only while the Assistant identity remains unbound."""
    return _install_local_snapshot(self, team_id, image_id, fresh_only=True)


def _install_local_snapshot(self, team_id: str, image_id: str, *, fresh_only: bool) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    admitted = _admit_local_snapshot(self, image_id)
    assistant_id = admitted.record["assistant_id"]
    existing = self.registry.binding(team_id, assistant_id)
    candidate = bindings.binding_from_local_record(team_id, admitted.record, snapshots.validate_record)
    try:
        self.assistant_icons.put_local(admitted.record, admitted.icon)
        if fresh_only and existing is not None:
            raise bindings.DynamicAssistantConflictError("automatic Local install requires an unbound Assistant")
        if existing is not None and existing.provenance == "published":
            result = self.assistant_lifecycle.replace_published_with_local(
                team_id,
                existing,
                lambda install_assistant: _apply_local_snapshot(
                    self,
                    team_id,
                    None,
                    admitted.record,
                    install_assistant=install_assistant,
                ),
            )
            existing = None
        elif existing is None:
            result = self.assistant_lifecycle.install_fresh_local(
                team_id,
                assistant_id,
                lambda install_assistant: _apply_local_snapshot(
                    self,
                    team_id,
                    None,
                    admitted.record,
                    install_assistant=install_assistant,
                ),
            )
        else:
            result = _apply_local_snapshot(self, team_id, existing, admitted.record)
    except ApiProblem:
        _discard_local_icon(self, candidate)
        raise
    except bindings.DynamicAssistantError as exc:
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
    *,
    install_assistant: Callable[..., dict[str, object]] | None = None,
) -> dict[str, object]:
    assistant_id = str(record["assistant_id"])
    if existing is None:
        spec, binding, created = self.registry.put_local_with_status(team_id, record)
        installer = install_assistant or self.assistant_lifecycle.install_assistant
        try:
            return installer(team_id, spec.assistant_id)
        except ApiProblem as exc:
            if created and exc.code != "assistant-install-rollback-incomplete":
                self.registry.delete_if_matches(team_id, assistant_id, binding.binding_digest)
            raise
        except bindings.DynamicAssistantError:
            if created:
                self.registry.delete_if_matches(team_id, assistant_id, binding.binding_digest)
            raise
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
    except ApiProblem:
        raise
    except developers.DevelopersError as exc:
        raise _developers_problem(exc) from exc
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


def _developers_problem(exc: developers.DevelopersError) -> ApiProblem:
    if isinstance(exc, developers.PublicationNotInstallableError):
        return ApiProblem(
            HTTPStatus.NOT_FOUND,
            "Assistant publication is not installable",
            code="assistant-not-installable",
        )
    if isinstance(exc, developers.DevelopersProtocolError):
        return ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "Developers response violates the Assistant publication contract",
            code="developers-protocol-invalid",
        )
    return ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Developers is unavailable",
        code="developers-unavailable",
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
        spec, binding, created = self.registry.put_with_status(team_id, resolution)
        try:
            return self.assistant_lifecycle.install_assistant(
                team_id,
                spec.assistant_id,
                authorize_start=authorize_start,
            )
        except ApiProblem as exc:
            if created and exc.code != "assistant-install-rollback-incomplete":
                self.registry.delete_if_matches(team_id, assistant_id, binding.binding_digest)
            raise
        except developers.DevelopersError, bindings.DynamicAssistantError:
            if created:
                self.registry.delete_if_matches(team_id, assistant_id, binding.binding_digest)
            raise
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
