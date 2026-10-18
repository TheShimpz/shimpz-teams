"""The Brain's two Browser exchanges as Team admits them (ADR-0117 §5 and §6).

A `browser-required` turn names exactly its one Browser call and nothing beside it; every other turn names none. A
`browser-step` answer is one raw step, the fixed Browser model the Brain ran it on, and that call's usage.
"""

from dataclasses import dataclass

from inference import usage as brain_usage
from inference.errors import BrainRuntimeError
from protocol.http.v1 import browser as http_browser
from protocol.http.v1 import turn as http_turn


@dataclass(frozen=True, slots=True)
class BrowserRequest:
    """The chat agent's one `browser` call: its interrupt and its self-contained objective."""

    interrupt_id: str
    objective: str


def parse_call(value: dict[str, object]) -> BrowserRequest | None:
    """A `browser-required` turn's one Browser call, which nothing accompanies; every other turn carries none."""
    browser = value["browser"]
    if value["status"] != "browser-required":
        if browser is not None:
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        return None
    if (
        not isinstance(browser, dict)
        or set(browser) != {"interrupt_id", "objective"}
        or not isinstance(browser["interrupt_id"], str)
        or http_turn.SAFE_ID_RE.fullmatch(browser["interrupt_id"]) is None
        or http_browser.canonical_objective(browser["objective"]) != browser["objective"]
        or value["reply"]
        or value["actions"]
        or value["clarification"] is not None
        or value["memory"]
        or value["routine"] is not None
    ):
        raise BrainRuntimeError("Brain runtime returned an invalid response")
    return BrowserRequest(browser["interrupt_id"], browser["objective"])


def parse_step(response: object) -> tuple[object, str, dict[str, int]]:
    """A `browser-step` answer's raw step, the Browser model that answered it, and the call's usage counts."""
    if not isinstance(response, dict) or set(response) != {"step", "model", "usage"}:
        raise BrainRuntimeError("Brain runtime returned an invalid response")
    model = response["model"]
    if not isinstance(model, str) or http_turn.SAFE_ID_RE.fullmatch(model) is None:
        raise BrainRuntimeError("Brain runtime returned an invalid response")
    try:
        counts = brain_usage.parse(response["usage"])
    except brain_usage.UsageError as exc:
        raise BrainRuntimeError("Brain runtime returned an invalid response") from exc
    return response["step"], model, counts
