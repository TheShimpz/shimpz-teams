"""Local Team inference-settings routes: the model selection."""

from local.http.dispatch import Operation, team_operation

OPERATIONS: dict[str, Operation] = {
    "inference-status": team_operation(lambda handler, team_id: handler.server.controller.inference_status(team_id)),
    "inference-configure": team_operation(
        lambda handler, team_id: handler.server.controller.configure_inference(team_id, handler.body())
    ),
}
