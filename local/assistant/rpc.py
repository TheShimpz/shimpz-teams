"""Local Assistant RPC transport and fail-stop readiness."""

import time
from contextlib import suppress
from http import HTTPStatus
from pathlib import Path

from docker.errors import DockerException, NotFound

from action import execution as action_execution
from local.errors import ApiProblemError as ApiProblem
from local.install.runtime import AssistantSpec

HEALTH_TIMEOUT_SECONDS = 15
ASSISTANT_WORKDIR = str(Path("/") / "tmp")


def _close_exec_stream(stream) -> None:
    action_execution.close_exec_stream(stream)


def _fail_stop_action(self, container) -> None:
    """Stop, then kill if needed, and prove an ambiguous local Action cannot keep running."""
    try:
        container.stop(timeout=3)
    except NotFound:
        return
    except DockerException:
        pass
    if self._action_not_running(container):
        return
    try:
        container.kill()
    except NotFound:
        return
    except DockerException:
        pass
    if self._action_not_running(container):
        return
    self._blocked_action_workloads.add(container.id)
    raise ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Assistant Action termination could not be proved; reinstall the Assistant",
        code="assistant-action-blocked",
    )


def _action_not_running(container) -> bool:
    try:
        container.reload()
    except NotFound:
        return True
    except DockerException:
        return False
    state = container.attrs.get("State")
    return isinstance(state, dict) and state.get("Running") is False


def _rpc(
    self,
    container,
    action_id: str,
    payload: dict,
) -> object:
    try:
        encoded = action_execution.encode_rpc_invocation(
            payload["input"],
            payload["integrations"],
            payload.get("responses", ()),
        )
    except (KeyError, ValueError) as exc:
        raise ApiProblem(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "request is too large",
            code="body-too-large",
        ) from exc

    def close_stream(stream: object) -> None:
        with suppress(Exception):
            self._close_exec_stream(stream)

    try:
        return action_execution.rpc_exchange(
            container.id,
            [action_execution.ACTION_COMMAND, action_id],
            encoded,
            action_execution.RpcExchangeStrategy(
                api=self.client.api,
                user=action_execution.ASSISTANT_RPC_USER,
                workdir=ASSISTANT_WORKDIR,
                timeout=action_execution.RPC_TIMEOUT_SECONDS,
                maximum=action_execution.MAX_RPC_RESPONSE_BYTES,
                transport_errors=(DockerException,),
                fail_stop=lambda: self._fail_stop_action(container),
                cancelled=lambda _exc: None,
                close_stream=close_stream,
            ),
        )
    except action_execution.RpcExchangeError as exc:
        message, code = action_execution.rpc_failure_message(exc.kind)
        status = action_execution.rpc_failure_status(exc.kind)
        raise ApiProblem(status, message, code=code) from exc


def _wait_ready(self, container, _spec: AssistantSpec) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "running":
            return
        if container.status != "created":
            break
        time.sleep(0.2)
    raise ApiProblem(HTTPStatus.BAD_GATEWAY, "Assistant did not become ready", code="assistant-not-ready")
