# Browser egress

This is the Team Browser's only route to the internet (ADR-0117). One static `internal` network with an isolated
gateway, no IPv6, and no external name resolution holds `shimpz-browser-egress` and at most one Browser container at
a time; only the proxy joins an outbound network.

The proxy accepts `CONNECT` to any public DNS hostname on port 443. It refuses every other port, every IP literal,
and every malformed or single-label name, and it never names a refused target in its audit. The neutral transport
(the `egress-transport` crate in `.egress/transport`, ADR-0104), which this directory's Rust package is built with,
resolves only public addresses, refuses a mixed public and private answer, records
the decision durably before any upstream connection attempt, and after the `200` relays nothing until the
Browser's first TLS ClientHello names exactly the admitted host with no ECH.

Every audit line carries the Browser's address and the random generation this process drew at start. Team restarts
the process between Browser tasks and records which generation each task ran on, so every line belongs to exactly
one task. Lines never carry a path, query, header, or page content.

The profile is packaged only by the root `ghcr.io/theshimpz/shimpz-egress` assembly under its closed `browser` profile
and runs as its own uid (10032). It receives no Team policy, credential, or state.
