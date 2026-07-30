"""Runtime-only Assistant fixture; no product registry or installation authority."""

from assistant_human import assistant_registry
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
    powers = {
        "list-zones": assistant_registry.PowerSpec(
            summary="List zones",
            input_schema=_PAGE,
            output_schema=_LIST_ZONES_OUTPUT,
            accounts=("cloudflare",),
        ),
        "list-dns-records": assistant_registry.PowerSpec(
            summary="List DNS records",
            input_schema=_DNS_PAGE,
            output_schema=_LIST_DNS_OUTPUT,
            accounts=("cloudflare",),
        ),
    }
    accounts = {
        "cloudflare": assistant_registry.AccountSpec(
            provider="cloudflare",
            scopes=("dns.read", "offline_access", "zone.read"),
        )
    }
    return AssistantSpec(
        assistant_id="shimpz-cloudflare",
        name="Shimpz Cloudflare",
        summary="Cloudflare test fixture",
        image=image,
        powers=powers,
        allowed_hosts=("api.cloudflare.com",),
        accounts=accounts,
        machine_contract={
            "version": 1,
            "powers": [
                {
                    "id": power_id,
                    "input_schema": dict(power.input_schema),
                    "output_schema": dict(power.output_schema),
                    "accounts": list(power.accounts),
                }
                for power_id, power in sorted(powers.items())
            ],
        },
    )


def hosted_spec(image: str) -> assistant_registry.AssistantSpec:
    local = assistant_spec(image)
    return assistant_registry.AssistantSpec(
        image=image,
        allowed_hosts=local.allowed_hosts,
        archs=("amd64", "arm64"),
        required_image_labels=(
            ("org.shimpz.assistant.id", local.assistant_id),
            ("org.shimpz.source.digest", "sha256:" + ("c" * 64)),
        ),
        contract=assistant_registry.AssistantContract(
            powers=local.powers,
            accounts=local.accounts,
            machine_contract=local.machine_contract,
        ),
    )
