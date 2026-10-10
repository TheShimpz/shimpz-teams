"""Team's confirmation policy for mutating Actions and the input projection every confirmation card shows."""

import json
from collections.abc import Mapping

from action import human as action_human
from protocol.http.v1 import payload as http_payload

# The one principal that confirms a Local Action: the Space's Supervisor, never an Assistant or the Brain.
PRINCIPAL = "local-supervisor"


def required(action_spec: object, enabled: bool) -> bool:
    """Whether the policy confirms this Action before it runs.

    Only a reviewed ``mutating`` Action that declares no authorization capability is confirmed; one that declares an
    authorization keeps its own single ceremony (ADR-0046).
    """
    return (
        enabled
        and getattr(action_spec, "effect", "mutating") == "mutating"
        and action_human.AUTHORIZATION_KINDS.isdisjoint(getattr(action_spec, "human_requests", ()))
    )


def request(
    team_id: str,
    binding: tuple[str, str, str],
    action: str,
    interrupt_id: str,
    arguments: Mapping[str, object],
) -> action_human.HumanRequest:
    """The confirmation request bound to the policy, principal, Team, exact binding, Action, and arguments.

    ``binding`` is the Assistant id, its immutable image, and its container; any change to it or to one argument is
    a different request, so an earlier confirmation never authorizes it.
    """
    assistant_id, image, container_id = binding
    return action_human.confirmation_request(
        {
            "policy": action_human.CONFIRMATION_POLICY,
            "principal": PRINCIPAL,
            "team_id": team_id,
            "assistant_id": assistant_id,
            "image": image,
            "container_id": container_id,
            "action": action,
            "interrupt_id": interrupt_id,
            "input": dict(arguments),
        }
    )


def input_projection(arguments: Mapping[str, object]) -> dict[str, object]:
    """Render the validated input as the bounded, escaped rows a confirmation card shows.

    Each top-level argument becomes one row: its escaped name and its escaped canonical JSON value, so a string is
    quoted and can never pass for a number, a boolean, or another argument. A row cut to its bound says so, and the
    arguments past the last row are counted; nothing is dropped silently.
    """
    rows = sorted(
        (_escaped(json.dumps(name, ensure_ascii=False))[1:-1], _value(value)) for name, value in arguments.items()
    )
    fields = []
    for name, value in rows[: http_payload.INPUT_PROJECTION_MAX_FIELDS]:
        shown_name, name_cut = _bounded(name, http_payload.INPUT_PROJECTION_NAME_CHARS)
        shown_value, value_cut = _bounded(value, http_payload.INPUT_PROJECTION_VALUE_CHARS)
        fields.append({"name": shown_name, "value": shown_value, "truncated": name_cut or value_cut})
    projection = {"fields": fields, "omitted": len(rows) - len(fields)}
    if http_payload.canonical_input_projection(projection) is None:
        raise ValueError("Action input cannot be projected")
    return projection


def _value(value: object) -> str:
    return _escaped(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": ")))


def _escaped(text: str) -> str:
    """JSON text with every character that is not printable, such as a bidirectional control, made visible."""
    return "".join(character if character.isprintable() else _escape(character) for character in text)


def _escape(character: str) -> str:
    code = ord(character)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def _bounded(text: str, maximum: int) -> tuple[str, bool]:
    """``text`` cut to ``maximum`` characters, never inside an escape, and whether it was cut."""
    if len(text) <= maximum:
        return text, False
    cut = text[:maximum]
    escape = cut.rfind("\\u")
    # An escape sequence the bound split is dropped whole; an escaped backslash ("\\\\u") is ordinary text.
    if escape != -1 and len(cut) - escape < 6 and not _escaped_backslash(cut, escape):
        cut = cut[:escape]
    return cut, True


def _escaped_backslash(text: str, index: int) -> bool:
    """Whether the backslash at ``index`` is itself escaped by an odd run of backslashes before it."""
    run = 0
    while index - run - 1 >= 0 and text[index - run - 1] == "\\":
        run += 1
    return run % 2 == 1
