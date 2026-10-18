#!/usr/local/bin/python3
"""CONNECT-only outbound enforcement for the Team Browser (ADR-0117).

The image's neutral CONNECT transport (`connect`, ADR-0104) resolves only public addresses, records, connects,
admits the TLS ClientHello, and splices; this profile owns only its admission (any public hostname on port 443), its
audit record, and its resource envelope. It holds no Team policy, credential, or state: one static isolated network
carries at most one Browser at a time, and Team restarts this process between Browser tasks.
"""

import ipaddress
import re

import audit
import connect

LISTEN_PORT = 3128
ALLOWED_PORT = 443
MAX_HOST_LENGTH = 253
# Chromium keeps at most 32 sockets per proxy; the bound leaves room for the loopback healthcheck.
MAX_CONCURRENCY = 40
MAX_SOURCE_CONCURRENCY = 40
LISTEN_BACKLOG = 40
# One LDH label: letters, digits, and inner hyphens, at most 63 characters.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def hostname(host: str) -> str | None:
    """Return the canonical DNS hostname of a CONNECT target, or None for an IP literal or a malformed name.

    A name needs at least two labels and a final label that is not all digits, so no dotted or bare spelling of an
    IPv4 address passes, and an IPv6 literal's colons and brackets fail the label rule.
    """
    canonical = host.lower()
    if not canonical or len(canonical) > MAX_HOST_LENGTH:
        return None
    labels = canonical.split(".")
    if len(labels) < 2 or labels[-1].isdigit() or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    return canonical


class Handler(connect.ConnectHandler):
    connect_timeout = 15
    idle_timeout = 120
    request_limit = 4096

    def admit(self, request: bytes) -> connect.Target | connect.Decision:
        """Admit a CONNECT to a public hostname on port 443; every other request is refused unnamed."""
        client = {"client": str(self.client_address[0])}
        target = connect.parse_connect(request)
        if isinstance(target, connect.Decision):
            return connect.refuse(target.code, "request-rejected", audit.REJECTED, **client)
        host = hostname(target.host)
        if host is None or target.port != ALLOWED_PORT:
            return connect.refuse(403, "destination-rejected", audit.REJECTED, **client)
        return connect.Target(host, ALLOWED_PORT, client)

    @classmethod
    def record(cls, decision: connect.Decision) -> None:
        audit.record(decision)


class Server(connect.BoundedServer):
    max_concurrency = MAX_CONCURRENCY
    max_source_concurrency = MAX_SOURCE_CONCURRENCY
    request_queue_size = LISTEN_BACKLOG


def main() -> int:
    try:
        audit.AUDIT.ensure_custody()
        server = Server((str(ipaddress.IPv4Address(0)), LISTEN_PORT), Handler)
    except OSError:
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
