"""The Brain's one automatic recovery decision for a held Routine run, as Team asks it (ADR-0092 section 6).

Team asks only after its own evidence proved the failed step had no effect, and only with what the decision needs: the
Routine's name and request, the step's Assistant Action, Team's proof, and the step's sanitized diagnostics. The answer
is one closed word; Team alone decides whether a retry is permitted and repeats only the same logical operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from inference.client import BrainRuntimeError, provider_credential
from protocol.http.v1 import payload as http_payload

DECISIONS = frozenset({"retry", "ask", "pause"})
PROOFS = frozenset({"not_occurred", "no_effect"})
MAX_DIAGNOSTICS = 8


def decide(
    client: object,
    credentials: tuple[str, str, str],
    locale: str | None,
    subject: Mapping[str, object],
    diagnostics: Sequence[Mapping[str, object]],
) -> str:
    """``credentials`` is the provider, model, and key; ``subject`` names the routine, step, and Team's proof."""
    provider, model, api_key = credentials
    body = provider_credential(provider, model, api_key)
    if (
        body is None
        or (locale is not None and http_payload.canonical_locale(locale) is None)
        or subject.get("proof") not in PROOFS
        or len(diagnostics) > MAX_DIAGNOSTICS
    ):
        raise BrainRuntimeError("Brain runtime Routine recovery request is invalid")
    payload = {
        "provider": body,
        "locale": locale,
        "routine": dict(subject["routine"]),
        "step": dict(subject["step"]),
        "proof": subject["proof"],
        "diagnostics": [dict(item) for item in diagnostics],
    }
    answer = client.routine_recovery(payload, provider, model)
    decision = answer.get("decision") if isinstance(answer, dict) and set(answer) == {"decision"} else None
    if decision not in DECISIONS:
        raise BrainRuntimeError("Brain runtime returned an invalid response")
    return decision
