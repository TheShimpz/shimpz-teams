"""Local Team Routine routes (ADR-0086).

Admin's scheduler claims runs and delivers notices under the Team bearer; its routine identity runs one leased run
under a routine assertion; a Supervisor session manages Routines, answers or stops their runs, and settles held runs
through their recovery cards (ADR-0092).
"""

from __future__ import annotations

import re
import time
from http import HTTPStatus

from action import human as action_human
from chat import progress as chat_progress
from core.http import strict as strict_http
from local import audit as local_audit
from local import authority as local_authority
from local.errors import ApiProblemError as ApiProblem
from local.http import stream as local_http_stream
from local.http.audit import RequestAudit
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine

MAX_BODY_BYTES = 16 * 1024
MAX_HUMAN_RESPONSE_BODY_BYTES = 128 * 1024
MACHINE_OPERATIONS = frozenset({"routine-claim", "routine-notices", "routine-notice-ack"})
RUN_OPERATION = "routine-run"
STREAMED_OPERATIONS = frozenset({"routine-human-submit", "routine-integration-submit"})
MODEL_BOUND_OPERATIONS = frozenset({RUN_OPERATION, *STREAMED_OPERATIONS})
BODY_LIMITS = {
    "routine-claim": MAX_BODY_BYTES,
    "routine-notice-ack": 64 * 1024,
    RUN_OPERATION: MAX_BODY_BYTES,
    "routine-challenge-open": MAX_BODY_BYTES,
    "routine-human-submit": MAX_HUMAN_RESPONSE_BODY_BYTES,
    "routine-integration-submit": MAX_BODY_BYTES,
    "routine-stop": MAX_BODY_BYTES,
    "routine-card-open": MAX_BODY_BYTES,
    "routine-card-answer": MAX_BODY_BYTES,
    "routine-resume": MAX_BODY_BYTES,
}
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


def _empty(handler, operation: str) -> None:
    if handler._body(max_bytes=BODY_LIMITS[operation]) != {}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "request requires an empty object", code="invalid-body")


def _run_id(route: strict_http.ControllerRouteMatch) -> str:
    run_id = route.params["run_id"]
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine run is unavailable", code="routine-run-not-found")
    return run_id


def _machine(handler, operation: str) -> dict[str, object]:
    service = handler.server.controller.chat_turn_service
    if operation == "routine-claim":
        claim = http_routine.canonical_claim_request(handler._body(max_bytes=BODY_LIMITS[operation]))
        if claim is None:
            raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "Routine claim is invalid", code="invalid-body")
        providers = tuple(claim["providers"])
        run = service.claim_routine_run(providers)
        return {"run": run, "next_due_at": None if run is not None else service.next_routine_due(providers)}
    if operation == "routine-notices":
        return service.routine_notices()
    return service.acknowledge_routine_notices(handler._body(max_bytes=BODY_LIMITS[operation]))


def _run(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """A Supervisor's decision on one run: open its challenge, or stop it."""
    service = handler.server.controller.chat_turn_service
    run_id = _run_id(route)
    body = handler._body(max_bytes=BODY_LIMITS[route.operation])
    if route.operation == "routine-challenge-open":
        # The challenge renders its request copy in the Admin interface language (ADR-0091).
        opening = http_routine.canonical_challenge_open(body)
        if opening is None:
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY, "opening a challenge requires only locale", code="invalid-body"
            )
        return service.open_routine_challenge(team_id, run_id, opening["locale"])
    if body != {}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "request requires an empty object", code="invalid-body")
    return service.stop_routine(team_id, run_id)


def _incident_id(route: strict_http.ControllerRouteMatch) -> str:
    incident_id = route.params["incident_id"]
    if _RUN_ID_RE.fullmatch(incident_id) is None:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine incident is unavailable", code="routine-incident-unavailable")
    return incident_id


def _card(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """A person's recovery card of one held run: open it with an empty body, or answer it once."""
    service = handler.server.controller.chat_turn_service
    incident_id = _incident_id(route)
    if route.operation == "routine-card-open":
        _empty(handler, route.operation)
        return service.open_routine_card(team_id, incident_id)
    return service.answer_routine_card(team_id, incident_id, handler._body(max_bytes=BODY_LIMITS[route.operation]))


def _resume(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    _empty(handler, route.operation)
    return handler.server.controller.chat_turn_service.resume_routine(team_id, route.params["routine_id"])


def _session(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """A Supervisor's management of the Team's Routines; run decisions go to ``_run``."""
    service = handler.server.controller.chat_turn_service
    operations = {
        "routine-list": lambda: service.list_routines(team_id),
        "routine-diagnostics": lambda: service.routine_run_diagnostics(team_id, _run_id(route), int(time.time())),
        "routine-delete": lambda: service.delete_routine(team_id, route.params["routine_id"]),
        "routine-card-open": lambda: _card(handler, route, team_id),
        "routine-card-answer": lambda: _card(handler, route, team_id),
        "routine-resume": lambda: _resume(handler, route, team_id),
    }
    operation = operations.get(route.operation)
    return operation() if operation is not None else _run(handler, route, team_id)


def route(
    handler, route: strict_http.ControllerRouteMatch
) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None]:
    """Every non-streamed Routine route, already authorized by its own authority."""
    if route.operation in MACHINE_OPERATIONS:
        return HTTPStatus.OK, _machine(handler, route.operation), route.operation, None, None
    team_id = validate_team_id(route.params["team_id"])
    return HTTPStatus.OK, _session(handler, route, team_id), route.operation, team_id, None


def stream(handler, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
    """A Supervisor's answer or Integration resume of a frozen run, streamed like the chat turn it continues."""
    team_id = validate_team_id(route.params["team_id"])
    service = handler.server.controller.chat_turn_service

    def execute(reporter: chat_progress.Reporter) -> tuple[HTTPStatus, dict[str, object]]:
        run_id = _run_id(route)
        provider, api_key = handler._model_credential_headers()
        if route.operation == "routine-human-submit":
            body = handler._body(max_bytes=BODY_LIMITS[route.operation])
            return HTTPStatus.OK, service.resume_routine_human(team_id, run_id, body, provider, api_key, reporter)
        _empty(handler, route.operation)
        return HTTPStatus.OK, service.resume_routine_integrations(team_id, run_id, provider, api_key, reporter)

    local_http_stream.respond(handler, route.operation, team_id, request_audit, execute)


def expected_assurance(handler, params: dict[str, str]) -> dict[str, str] | None:
    """The assurance an approval of a frozen run's authentication request must carry, as in chat."""
    body = handler._body(max_bytes=MAX_HUMAN_RESPONSE_BODY_BYTES)
    if set(body) != {"challenge_id", "decision", "value"} or body["decision"] != "submit" or body["value"] is not True:
        return None
    team_id = validate_team_id(params.get("team_id"))
    challenge = handler.server.controller.chat_turn_service.current_routine_challenge(team_id)
    if (
        challenge is None
        or challenge.id != body["challenge_id"]
        or challenge.requirement.request.kind not in action_human.AUTH_KINDS
    ):
        return None
    return {"kind": challenge.requirement.request.kind, "challenge_id": challenge.id}


def run(handler, parts: list[str], route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
    """One segment of a leased run under Admin's routine identity; its lease is checked by the run itself."""
    body = handler._capture_body(route.operation)
    try:
        evidence = local_authority.verify_routine(
            handler.headers,
            request=local_authority.RequestBinding(
                method=handler.command,
                path="/" + "/".join(parts),
                body=body,
                model=handler._model_binding(route.operation),
                assurance=None,
                authority_kinds=frozenset(),
            ),
        )
    except local_authority.SupervisorDeniedError as exc:
        request_audit.record("routine-authority", result="denied", detail="invalid-routine")
        raise ApiProblem(HTTPStatus.FORBIDDEN, "Routine authority is required", code="invalid-routine") from exc
    except local_authority.SupervisorUnavailableError as exc:
        request_audit.record("routine-authority", result="error", detail="routine-unavailable")
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Routine authority is unavailable", code="routine-unavailable"
        ) from exc
    request_audit.routine()
    request_audit.record("routine-authority", result="ok")
    team_id = validate_team_id(route.params["team_id"])
    run_id = _run_id(route)
    claimed = http_routine.canonical_segment_request(handler._body(max_bytes=BODY_LIMITS[route.operation]))
    if claimed is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY, "a segment names the claimed revision and plan", code="invalid-body"
        )
    service = handler.server.controller.chat_turn_service

    def execute(reporter: chat_progress.Reporter) -> tuple[HTTPStatus, dict[str, object]]:
        provider, api_key = handler._model_credential_headers()
        binding = (claimed["revision"], claimed["plan_digest"])
        return HTTPStatus.OK, service.run_routine(team_id, run_id, evidence, binding, (provider, api_key), reporter)

    with local_audit.bind_request_principal(request_audit.principal()):
        local_http_stream.respond(handler, route.operation, team_id, request_audit, execute)
