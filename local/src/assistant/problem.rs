//! The public problems of Assistant containers, each with one status, message, and code.

use crate::http::service::Problem;

/// The container drifted from its isolation profile, ownership labels, name, or image lineage.
pub const ISOLATION_DRIFT: Problem = Problem::new(
    409,
    "the installed Assistant failed its isolation profile",
    "assistant-isolation-drift",
);
/// A container names an Assistant the Team has no binding for, or its reviewed contract is invalid.
pub const REGISTRY_DRIFT: Problem = Problem::new(
    409,
    "an installed Assistant is no longer allowlisted",
    "assistant-registry-drift",
);
/// The reviewed `allowed_hosts` of a binding are not canonical.
pub const ALLOWED_HOSTS_INVALID: Problem = Problem::new(
    409,
    "the reviewed Assistant allowed_hosts contract is invalid",
    "assistant-registry-drift",
);
/// No container of that Assistant exists.
pub const NOT_FOUND: Problem =
    Problem::new(404, "Assistant is not installed", "assistant-not-found");
/// The image provenance is not the one this operation takes.
pub const PUBLISHED_PROVENANCE_REQUIRED: Problem = Problem::new(
    409,
    "the Assistant does not have published image provenance",
    "image-provenance-mismatch",
);
/// The image provenance is not the one this operation takes.
pub const LOCAL_PROVENANCE_REQUIRED: Problem = Problem::new(
    409,
    "the Assistant does not have Local image provenance",
    "image-provenance-mismatch",
);
/// A trusted image could not be pulled.
pub const IMAGE_PULL_FAILED: Problem = Problem::new(
    502,
    "the trusted Assistant image could not be pulled",
    "image-pull-failed",
);
/// A published image does not carry its reviewed digest and labels, or is a staged image.
pub const IMAGE_CONTRACT_MISMATCH: Problem = Problem::new(
    409,
    "the Assistant image does not match its trusted contract",
    "image-contract-mismatch",
);
/// A staged image is gone.
pub const LOCAL_IMAGE_MISSING: Problem = Problem::new(
    409,
    "the staged Local Assistant image is no longer available",
    "local-image-missing",
);
/// A staged image is not exactly the bound one.
pub const LOCAL_IMAGE_CONTRACT_MISMATCH: Problem = Problem::new(
    409,
    "the staged Local Assistant image does not match its binding",
    "local-image-contract-mismatch",
);
/// Docker created the container from another image than the one resolved.
pub const IMAGE_RESOLUTION_MISMATCH: Problem = Problem::new(
    409,
    "Docker resolved an unexpected Assistant image",
    "image-resolution-mismatch",
);
/// The container holds an older artifact than its binding.
pub const UPDATE_REQUIRED: Problem = Problem::new(
    409,
    "the installed Assistant must be updated",
    "assistant-update-required",
);
/// Docker refused the create, and no container is left.
pub const INSTALL_FAILED: Problem = Problem::new(
    503,
    "Docker could not install the Assistant",
    "docker-install-failed",
);
/// A failed install could not prove what it created gone; what is left keeps its egress policy for the next install.
pub const INSTALL_ROLLBACK_INCOMPLETE: Problem = Problem::new(
    500,
    "Assistant install rollback is incomplete",
    "assistant-install-rollback-incomplete",
);
/// The container did not run within its readiness bound.
pub const NOT_READY: Problem =
    Problem::new(502, "Assistant did not become ready", "assistant-not-ready");
/// Docker could not start an existing container.
pub const START_FAILED: Problem = Problem::new(
    503,
    "Docker could not start the Assistant",
    "docker-start-failed",
);
/// Docker could not remove the container.
pub const REMOVE_FAILED: Problem = Problem::new(
    503,
    "Docker could not uninstall the Assistant",
    "docker-remove-failed",
);
/// An Action's termination could not be proved; the container runs nothing more until it is reinstalled.
pub const ACTION_BLOCKED: Problem = Problem::new(
    503,
    "Assistant Action termination could not be proved; reinstall the Assistant",
    "assistant-action-blocked",
);
/// The egress policy failed its ownership contract.
pub const EGRESS_POLICY_DRIFT: Problem = Problem::new(
    409,
    "Assistant egress policy failed its ownership contract",
    "egress-policy-drift",
);
/// The egress policy store could not complete an otherwise valid operation.
pub const EGRESS_POLICY_UNAVAILABLE: Problem = Problem::new(
    503,
    "Assistant egress policy storage is unavailable",
    "egress-policy-unavailable",
);
