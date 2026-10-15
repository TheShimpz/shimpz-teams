"""Docker ownership labels for the local controller."""

from collections.abc import Mapping

MANAGED_LABEL = "com.shimpz.local.managed"
PROFILE_LABEL = "com.shimpz.local.profile"
SPACE_LABEL = "com.shimpz.local.space-id"
KIND_LABEL = "com.shimpz.local.kind"
TEAM_LABEL = "com.shimpz.local.team-id"
TEAM_NAME_LABEL = "com.shimpz.local.team-name"
ASSISTANT_LABEL = "com.shimpz.local.assistant-id"
IMAGE_LABEL = "com.shimpz.local.image"

# The ownership profile every Docker object this controller creates carries.
PROFILE = "local-v1"


def ownership_labels(space_id: str, kind: str, team_id: str | None = None) -> dict[str, str]:
    """The exact labels of one kind of Docker object this Space's controller owns, optionally for one Team."""
    labels = {MANAGED_LABEL: "1", PROFILE_LABEL: PROFILE, SPACE_LABEL: space_id, KIND_LABEL: kind}
    if team_id is not None:
        labels[TEAM_LABEL] = team_id
    return labels


def label_filter(labels: Mapping[str, str]) -> list[str]:
    """Docker's label filter that selects exactly the objects carrying every one of ``labels``."""
    return [f"{key}={value}" for key, value in labels.items()]


def labels_include(actual: object, expected: Mapping[str, str]) -> bool:
    return isinstance(actual, dict) and all(actual.get(key) == value for key, value in expected.items())
