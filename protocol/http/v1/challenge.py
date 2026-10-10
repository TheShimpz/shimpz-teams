"""The presentation a Team human-required challenge carries beside its canonical request (ADR-0091, ADR-0093).

The rendered copy of the request's catalog references, the one file an authorization discloses, and a confirmation's
projection of the validated Action input: display values a client shows and validates, never part of the request's
fingerprint.
"""

from __future__ import annotations

if __package__:
    from . import payload
else:  # The protocol verifier runs every module of this directory flat.
    import payload

# The rendered copy bounds of a human request's catalog references (Assistant Spec v1, ADR-0091).
RENDERED_FIELD_CHARS = {"title": 80, "description": 500, "label": 80, "placeholder": 120}
RENDERED_OPTION_CHARS = {"label": 80, "description": 160}


def canonical_rendered(value: object, request: object) -> dict[str, object] | None:
    """Return the rendered copy of exactly the canonical request's copy fields, or None (ADR-0091).

    The request keeps its catalog references, option values, and kind; this block carries only display text in the
    challenge's interface language, in the request's field and option order, within each field's bound.
    """
    if not isinstance(value, dict) or not isinstance(request, dict):
        return None
    fields = [field for field in RENDERED_FIELD_CHARS if field in request]
    expected = {*fields, *(("options",) if "options" in request else ())}
    if set(value) != expected or not all(
        payload._rendered(value[field], request[field], RENDERED_FIELD_CHARS[field], nullable=field == "placeholder")
        for field in fields
    ):
        return None
    if "options" in request and not _rendered_options(value["options"], request["options"]):
        return None
    return value


def _rendered_options(values: object, options: object) -> bool:
    return (
        isinstance(values, list)
        and isinstance(options, list)
        and len(values) == len(options)
        and all(
            isinstance(item, dict)
            and isinstance(option, dict)
            and set(item) == {"label", "description"}
            and payload._rendered(item["label"], option.get("label"), RENDERED_OPTION_CHARS["label"], nullable=False)
            and payload._rendered(
                item["description"], option.get("description"), RENDERED_OPTION_CHARS["description"], nullable=True
            )
            for item, option in zip(values, options, strict=True)
        )
    )


# The largest original an Action may receive (Assistant Spec v1, ADR-0093).
MAX_ACTION_FILE_BYTES = 8 * 1024 * 1024
FILE_DISCLOSURE_KEYS = frozenset({"id", "name", "media_type", "size", "sha256"})


def canonical_file_disclosure(value: object) -> dict[str, object] | None:
    """Return the one file an authorization challenge discloses, or None (ADR-0093).

    Its opaque id, literal filename, Team-determined media type, size, and original SHA-256 name exactly the selected
    file whose original bytes, with any metadata embedded in them, the approved replay delivers to the Action.
    """
    if not isinstance(value, dict) or set(value) != FILE_DISCLOSURE_KEYS:
        return None
    size = payload._integer(value["size"], minimum=1)
    if (
        payload.canonical_file_id(value["id"]) is None
        or payload.canonical_filename(value["name"]) is None
        or not isinstance(value["media_type"], str)
        or payload.canonical_media_type(value["media_type"]) != value["media_type"]
        or size is None
        or size > MAX_ACTION_FILE_BYTES
        or not isinstance(value["sha256"], str)
        or payload.SHA256_RE.fullmatch(value["sha256"]) is None
    ):
        return None
    return {key: value[key] for key in ("id", "name", "media_type", "size", "sha256")}


# The platform-rendered projection of a confirmation challenge's validated Action input (ADR-0093 file-card
# precedent): one row per top-level argument in name order, each the argument's escaped JSON text.
INPUT_PROJECTION_KEYS = frozenset({"fields", "omitted"})
INPUT_PROJECTION_FIELD_KEYS = frozenset({"name", "value", "truncated"})
INPUT_PROJECTION_MAX_FIELDS = 16
INPUT_PROJECTION_NAME_CHARS = 128
INPUT_PROJECTION_VALUE_CHARS = 400
INPUT_PROJECTION_MAX_OMITTED = 4096


def canonical_input_projection(value: object) -> dict[str, object] | None:
    r"""Return one confirmation challenge's projection of the validated Action input, or None.

    ``fields`` holds at most 16 rows in strictly ascending ``name`` order. ``name`` (1 to 128 characters) is the
    argument's escaped name and ``value`` (1 to 400) its escaped canonical JSON text; both are printable, and escaping
    makes every other character a visible ``\uXXXX``. A row whose name or value had to be cut to its bound has
    ``truncated`` true, and ``omitted`` counts the arguments past the sixteenth row, so the bounds never hide an
    argument silently: a client must show both.
    """
    if not isinstance(value, dict) or set(value) != INPUT_PROJECTION_KEYS:
        return None
    fields, omitted = value["fields"], value["omitted"]
    if (
        not isinstance(fields, list)
        or len(fields) > INPUT_PROJECTION_MAX_FIELDS
        or type(omitted) is not int
        or not 0 <= omitted <= INPUT_PROJECTION_MAX_OMITTED
        or (omitted and len(fields) != INPUT_PROJECTION_MAX_FIELDS)
    ):
        return None
    previous = None
    for field in fields:
        if (
            not isinstance(field, dict)
            or set(field) != INPUT_PROJECTION_FIELD_KEYS
            or not _projected_text(field["name"], INPUT_PROJECTION_NAME_CHARS)
            or not _projected_text(field["value"], INPUT_PROJECTION_VALUE_CHARS)
            or type(field["truncated"]) is not bool
            or (previous is not None and field["name"] <= previous)
        ):
            return None
        previous = field["name"]
    return value


def _projected_text(text: object, maximum: int) -> bool:
    return isinstance(text, str) and 0 < len(text) <= maximum and text.isprintable()
