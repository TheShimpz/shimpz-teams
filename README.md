# Shimpz Teams

`shimpz-teams` owns the Team domain: Team and Assistant authority, isolation, authorization, lifecycle, and the
Docker-mediated workload boundary.

The repository is consumed by the Shimpz umbrella at the root `teams/` checkout. It is not a Service repository and
does not own Brain, Account, Assistant-release, or egress-proxy responsibilities.

## Source organization

- `assistant/`, `chat/`, `egress/`, `inference/`, `install/`, `integrations/`, `power/`, and `storage/` are
  profile-neutral Team responsibilities.
- `core/` contains only cohesive invariants shared by both profiles: strict JSON, HTTP parsing, container identity,
  isolation, and network policy.
- `hosted/` owns the Hosted entrypoint, state, authority, audit, validation, token, and container construction.
  Its `assistant/`, `chat/`, `http/`, `install/`, and `team/` children separate runtime responsibilities.
- `local/` owns the Local entrypoint, state, audit, validation, token, labels, and lifecycle used by
  `install.shimpz.com`. Its `assistant/`, `chat/`, `http/`, and `install/` children express the same profile
  ownership without sharing materially different controller code with Hosted.
- `install/` owns profile-neutral publication verification and binding; `hosted/install/` owns Hosted-only
  authorization and materialization adapters.
- `protocol/http/` is Team's HTTP authority; `protocol/assistant/` and `protocol/install/` are exact pinned mirrors
  used for independent admission and installation conformance.
- `egress/` owns Team policy and bindings; the enforcement proxy remains in the Assistant domain.
- The repository root contains governance and dependency metadata only; runtime Python belongs to a named
  responsibility or profile.

Directory names use the shortest clear responsibility term, such as `install/`. A peer domain may appear in a leaf
adapter name but never as a child domain owned by Team.

## Local validation

Use Python 3.14 and the committed dependency lock:

```bash
ruff check --config ruff.toml .
uv run --frozen --python 3.14 python -m unittest discover -s tests -p "test_*.py"
```
