"""Runtime-only Assistant fixture; no product registry or installation authority."""

from assistant import spec as assistant_registry
from local.install.runtime import AssistantSpec

_PAGINATION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        key: {"type": "integer", "minimum": 0} for key in ("page", "per_page", "count", "total_count", "total_pages")
    },
    "required": ["page", "per_page", "count", "total_count", "total_pages"],
}
_PAGE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "page": {"type": "integer", "minimum": 1},
        "per_page": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "required": ["page", "per_page"],
}
_DNS_PAGE = {
    **_PAGE,
    "properties": {
        **_PAGE["properties"],
        "zone_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    },
    "required": ["zone_id", "page", "per_page"],
}
_LIST_ZONES_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "zones": {"type": "array", "items": {"type": "object"}},
        "pagination": _PAGINATION,
    },
    "required": ["zones", "pagination"],
}
_LIST_DNS_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "records": {"type": "array", "items": {"type": "object"}},
        "pagination": _PAGINATION,
    },
    "required": ["records", "pagination"],
}


def assistant_spec(image: str) -> AssistantSpec:
    actions = {
        "list-zones": assistant_registry.ActionSpec(
            summary="List zones",
            input_schema=_PAGE,
            output_schema=_LIST_ZONES_OUTPUT,
            integrations=("cloudflare",),
        ),
        "list-dns-records": assistant_registry.ActionSpec(
            summary="List DNS records",
            input_schema=_DNS_PAGE,
            output_schema=_LIST_DNS_OUTPUT,
            integrations=("cloudflare",),
        ),
    }
    integrations = {
        "cloudflare": assistant_registry.IntegrationSpec(
            provider="cloudflare",
            scopes=("dns.read", "offline_access", "zone.read"),
        )
    }
    return AssistantSpec(
        assistant_id="shimpz-cloudflare",
        version="0.4.1",
        name="Shimpz Cloudflare",
        summary="Cloudflare test fixture",
        image=image,
        actions=actions,
        allowed_hosts=("api.cloudflare.com",),
        required_image_labels=(
            ("org.shimpz.assistant.id", "shimpz-cloudflare"),
            ("org.shimpz.source.digest", "sha256:" + ("c" * 64)),
        ),
        integrations=integrations,
        machine_contract={
            "version": 1,
            "actions": [
                {
                    "id": action_id,
                    "input_schema": dict(action.input_schema),
                    "output_schema": dict(action.output_schema),
                    "integrations": list(action.integrations),
                    "human_requests": list(action.human_requests),
                }
                for action_id, action in sorted(actions.items())
            ],
        },
    )


def hosted_spec(image: str) -> assistant_registry.AssistantSpec:
    local = assistant_spec(image)
    return assistant_registry.AssistantSpec(
        version=local.version,
        summary=local.summary,
        image=image,
        allowed_hosts=local.allowed_hosts,
        archs=("amd64", "arm64"),
        required_image_labels=(
            ("org.shimpz.assistant.id", local.assistant_id),
            ("org.shimpz.source.digest", "sha256:" + ("c" * 64)),
        ),
        contract=assistant_registry.AssistantContract(
            name=local.name,
            actions=local.actions,
            integrations=local.integrations,
            machine_contract=local.machine_contract,
        ),
    )
