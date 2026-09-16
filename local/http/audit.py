"""Per-request audit state for the Local Team HTTP boundary."""

from dataclasses import dataclass

from local import audit as local_audit
from local import authority as local_authority


@dataclass(slots=True)
class RequestAudit:
    operation: str = "request"
    principal_id: str | None = None
    principal_class: str = "absent"
    credential_state: str = "machine_bearer_present"
    trace_id: str | None = None

    def machine(self) -> None:
        self.principal_id = "admin"
        self.principal_class = "machine"
        self.credential_state = "machine_bearer_present"

    def human(self, evidence: local_authority.Evidence) -> None:
        self.principal_id = evidence.supervisor_id
        self.principal_class = "human"
        self.credential_state = "assertion_present"

    def absent(self, credential_state: str) -> None:
        self.principal_id = None
        self.principal_class = "absent"
        self.credential_state = credential_state

    def principal(self) -> local_audit.AuditPrincipal:
        return local_audit.AuditPrincipal(
            principal_id=self.principal_id,
            principal_class=self.principal_class,
            credential_state=self.credential_state,
            trace_id=self.trace_id,
        )

    def record(
        self,
        operation: str,
        *,
        result: str,
        team_id: str | None = None,
        assistant: str | None = None,
        detail: str | None = None,
    ) -> str:
        selected_operation = self.operation if operation == "request" else operation
        self.trace_id = local_audit.record(
            selected_operation,
            result=result,
            principal=self.principal(),
            team_id=team_id,
            assistant=assistant,
            detail=detail,
        )
        return self.trace_id
