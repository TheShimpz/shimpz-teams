# Shimpz Teams

`shimpz-teams` owns the Team domain: Team and Assistant authority, isolation, authorization, lifecycle, and the
Docker-mediated workload boundary.

The repository is consumed by the Shimpz umbrella at the root `teams/` checkout. It is not a Service repository and
does not own Brain, Account, Assistant-release, or egress-proxy responsibilities.

## Source organization

- `hosted/` contains the Hosted entrypoint, healthcheck, and Hosted-only responsibilities.
- `install/` owns profile-neutral publication contracts; `hosted/install/` owns Hosted-only authorization and
  materialization adapters.
- `install/protocol/` is the verified wire-protocol mirror consumed by both Team profiles.
- `local/` contains the Local entrypoint, healthcheck, and image definition used by `install.shimpz.com`.
- `chat/`, `egress/`, `inference/`, and `power/` are profile-neutral Team responsibilities.
- `egress/` owns Team policy and bindings; the enforcement proxy remains in the Assistant domain.
- `core/` contains only low-level Team invariants shared by both profiles, currently strict JSON and HTTP contracts.
- Remaining root packages are unclassified transitional source, not a precedent for placing new files at root.

Directory names use the shortest clear responsibility term, such as `install/`. A peer domain may appear in a leaf
adapter name but never as a child domain owned by Team.

## Local validation

Use Python 3.14 and the committed dependency lock:

```bash
ruff check --config ruff.toml .
uv run --frozen --python 3.14 python -m unittest discover -s tests -p "test_*.py"
```
