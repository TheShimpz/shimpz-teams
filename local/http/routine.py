"""Local Team Routine routes (ADR-0086).

Admin's scheduler claims runs and delivers notices under the Team bearer; its routine identity runs one leased run
under a routine assertion; a Supervisor session confirms or cancels a recorded Routine's card (ADR-0101), manages
Routines, answers or stops their runs, and settles held runs through their recovery cards (ADR-0092).
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
from protocol.http.v1 import routine_notice as http_routine_notice
from protocol.http.v1 import routine_run as http_routine_run

MAX_BODY_BYTES = 16 * 1024
MAX_HUMAN_RESPONSE_BODY_BYTES = 128 * 1024
MACHINE_OPERATIONS = frozenset({"routine-claim", "routine-notices", "routine-notice-ack"})
RUN_OPERATION = "routine-run"
STREAMED_OPERATIONS = frozenset({"routine-human-submit", "routine-integration-submit"})
MODEL_BOUND_OPERATIONS = frozenset({RUN_OPERATION, *STREAMED_OPERATIONS})
# The Team's whole Routine list is the one response with its own protocol allowance; every other keeps the API cap.
RESPONSE_LIMITS = {"routine-list": http_routine_notice.MAX_ROUTINE_LIST_BYTES}
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
    "routine-pause": MAX_BODY_BYTES,
    "routine-proposal-confirm": MAX_BODY_BYTES,
}


def _empty(handler, operation: str) -> None:
    if handler._body(max_bytes=BODY_LIMITS[operation]) != {}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "request requires an empty object", code="invalid-body")


def _run_id(route: strict_http.ControllerRouteMatch) -> str:
    run_id = route.params["run_id"]
    if http_routine.ROUTINE_ID_RE.fullmatch(run_id) is None:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine run is unavailable", code="routine-run-not-found")
    return run_id


def _machine(handler, operation: str) -> dict[str, object]:
    service = handler.server.controller.chat_turn_service
    if operation == "routine-claim":
        request = http_routine_run.canonical_claim_request(handler._body(max_bytes=BODY_LIMITS[operation]))
        if request is None:
            raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "Routine claim is invalid", code="invalid-body")
        run = service.claim_routine_run(request["long"])
        return {"run": run, "next_due_at": None if run is not None else service.next_routine_due()}
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
        opening = http_routine_run.canonical_challenge_open(body)
        if opening is None:
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY, "opening a challenge requires only locale", code="invalid-body"
            )
        return service.open_routine_challenge(team_id, run_id, opening["locale"])
    if body != {}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "request requires an empty object", code="invalid-body")
    return service.stop_routine(team_id, run_id)


_COUNT_RE = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")


def _steps(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """One page of a Routine's steps, from an offset, for exactly the revision the reader names."""
    routine_id, revision, offset = (route.params[key] for key in ("routine_id", "revision", "offset"))
    if (
        http_routine.ROUTINE_ID_RE.fullmatch(routine_id) is None
        or _COUNT_RE.fullmatch(revision) is None
        or _COUNT_RE.fullmatch(offset) is None
        or not 1 <= int(revision) < 2**31
    ):
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine steps are unavailable", code="routine-steps-not-found")
    service = handler.server.controller.chat_turn_service
    return service.routine_steps(team_id, routine_id, int(revision), int(offset))


def _run_steps(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """One page of what a run did, from an offset, for exactly the snapshot of its records the reader holds."""
    run_id, snapshot, offset = _run_id(route), route.params["snapshot"], route.params["offset"]
    if (snapshot != "latest" and http_routine_run.SNAPSHOT_RE.fullmatch(snapshot) is None) or _COUNT_RE.fullmatch(
        offset
    ) is None:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine run steps are unavailable", code="routine-run-steps-not-found")
    service = handler.server.controller.chat_turn_service
    return service.routine_run_steps(team_id, run_id, snapshot, int(offset), int(time.time()))


def _incident_id(route: strict_http.ControllerRouteMatch) -> str:
    incident_id = route.params["incident_id"]
    if http_routine.ROUTINE_ID_RE.fullmatch(incident_id) is None:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine incident is unavailable", code="routine-incident-unavailable")
    return incident_id


def _card(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """A person's recovery card of one held run: open it with an empty body, or answer it once with Rodar or Excluir."""
    service = handler.server.controller.chat_turn_service
    incident_id = _incident_id(route)
    if route.operation == "routine-card-open":
        _empty(handler, route.operation)
        return service.open_routine_card(team_id, incident_id)
    return service.answer_routine_card(team_id, incident_id, handler._body(max_bytes=BODY_LIMITS[route.operation]))


def _pause(handler, route: strict_http.ControllerRouteMatch, team_id: str, paused: bool) -> dict[str, object]:
    """A person's Pausar or Retomar of a whole Routine, with an empty body."""
    _empty(handler, route.operation)
    service = handler.server.controller.chat_turn_service
    change = service.pause_routine if paused else service.resume_routine
    return change(team_id, route.params["routine_id"])


_PROPOSAL_RE = re.compile(r"[0-9a-f]{32}\Z")


def _proposal(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """A person's answer to a recorded Routine's card: Criar rotina with an empty body, or Cancelar as a DELETE."""
    proposal_id = route.params["proposal_id"]
    if _PROPOSAL_RE.fullmatch(proposal_id) is None:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine card is unavailable", code="routine-proposal-expired")
    service = handler.server.controller.chat_turn_service
    if route.operation == "routine-proposal-confirm":
        _empty(handler, route.operation)
        return service.confirm_routine_proposal(team_id, proposal_id)
    return service.revoke_routine_proposal(team_id, proposal_id)


def _session(handler, route: strict_http.ControllerRouteMatch, team_id: str) -> dict[str, object]:
    """A Supervisor's management of the Team's Routines; run decisions go to ``_run``."""
    service = handler.server.controller.chat_turn_service
    operations = {
        "routine-list": lambda: service.list_routines(team_id),
        "routine-steps": lambda: _steps(handler, route, team_id),
        "routine-run-steps": lambda: _run_steps(handler, route, team_id),
        "routine-diagnostics": lambda: service.routine_run_diagnostics(team_id, _run_id(route), int(time.time())),
        "routine-delete": lambda: service.delete_routine(team_id, route.params["routine_id"]),
        "routine-card-open": lambda: _card(handler, route, team_id),
        "routine-card-answer": lambda: _card(handler, route, team_id),
        "routine-resume": lambda: _pause(handler, route, team_id, False),
        "routine-pause": lambda: _pause(handler, route, team_id, True),
        "routine-proposal-confirm": lambda: _proposal(handler, route, team_id),
        "routine-proposal-revoke": lambda: _proposal(handler, route, team_id),
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
        provider, api_key = handler._model_credential(route.operation) or ("", "")
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
    claimed = http_routine_run.canonical_segment_request(handler._body(max_bytes=BODY_LIMITS[route.operation]))
    if claimed is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY, "a segment names the claimed revision and plan", code="invalid-body"
        )
    service = handler.server.controller.chat_turn_service

    def execute(reporter: chat_progress.Reporter) -> tuple[HTTPStatus, dict[str, object]]:
        provider, api_key = handler._model_credential(route.operation) or ("", "")
        binding = (claimed["revision"], claimed["plan_digest"], claimed["mode"])
        return HTTPStatus.OK, service.run_routine(team_id, run_id, evidence, binding, (provider, api_key), reporter)

    with local_audit.bind_request_principal(request_audit.principal()):
        local_http_stream.respond(handler, route.operation, team_id, request_audit, execute)
