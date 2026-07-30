"""Resolve immutable Assistant artifacts without accepting caller-controlled image references."""

from __future__ import annotations

import docker

from assistant import spec as assistant_registry

from . import registry_auth


class ImageTrustError(RuntimeError):
    """A reviewed digest artifact could not be obtained or its identity could not be proved."""


def _get(images, image_ref: str):
    try:
        return images.get(image_ref)
    except docker.errors.NotFound:
        return None
    except docker.errors.DockerException as exc:
        raise ImageTrustError("trusted Assistant artifact is unavailable") from exc


def ensure_digest_artifact(
    images,
    spec: assistant_registry.AssistantSpec,
    credentials: registry_auth.RegistryAuth,
) -> str:
    """Get or pull one registry-owned digest, then prove its digest and declared OCI identity."""
    image_ref = spec.image
    if not assistant_registry.is_digest_image(image_ref):
        raise ImageTrustError("Assistant artifact is not digest-pinned")

    image = _get(images, image_ref)
    if image is None:
        try:
            # image_ref is the exact reviewed registry digest from AssistantSpec. No request field can
            # influence this pull, and tags are deliberately rejected above.
            images.pull(
                image_ref,
                auth_config=credentials.docker_auth_config(),
            )
        except docker.errors.DockerException as exc:
            raise ImageTrustError("trusted Assistant artifact is unavailable") from exc
        image = _get(images, image_ref)
        if image is None:
            raise ImageTrustError("trusted Assistant artifact is unavailable")

    attrs = getattr(image, "attrs", None)
    repo_digests = attrs.get("RepoDigests") if isinstance(attrs, dict) else None
    config = attrs.get("Config") if isinstance(attrs, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    image_id = getattr(image, "id", None)
    if (
        not isinstance(repo_digests, list)
        or image_ref not in repo_digests
        or not isinstance(labels, dict)
        or not isinstance(image_id, str)
        or not image_id.startswith("sha256:")
        or any(labels.get(key) != value for key, value in spec.required_image_labels)
    ):
        raise ImageTrustError("trusted Assistant artifact identity does not match its registry contract")
    return image_id
