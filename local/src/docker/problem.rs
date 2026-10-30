//! The public problems of Docker failures and of Docker objects that fail their ownership contract, shared by every
//! owner of Team's Docker objects. None carries the daemon's text.

use std::borrow::Cow;

use crate::http::service::Problem;

/// A failure no public problem names (a store Team depends on failed); it answers without a code.
pub const INTERNAL: Problem = Problem {
    status: 500,
    message: Cow::Borrowed("internal error"),
    code: None,
    retry_after_ms: None,
};

/// The daemon could not answer; never read as the absence of an object.
pub const DOCKER_UNAVAILABLE: Problem =
    Problem::new(503, "Docker is unavailable", "docker-unavailable");
/// A Team object does not carry exactly its ownership labels, name, or attributes.
pub const OWNERSHIP_CONFLICT: Problem = Problem::new(
    409,
    "Team resource ownership conflict",
    "ownership-conflict",
);
/// A Team's resources failed their ownership contract during its destruction.
pub const TEAM_RESOURCES_OWNERSHIP_CONFLICT: Problem = Problem::new(
    409,
    "Team resources failed their ownership contract",
    "ownership-conflict",
);
/// A labelled Space resource failed its ownership contract during the Space reset.
pub const SPACE_RESOURCE_OWNERSHIP_CONFLICT: Problem = Problem::new(
    409,
    "a labeled Space resource failed its ownership contract",
    "ownership-conflict",
);
