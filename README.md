# Shimpz Teams

`shimpz-teams` owns the Team domain: Team and Assistant authority, isolation, authorization, lifecycle, and the
Docker-mediated workload boundary.

The repository is consumed by the Shimpz umbrella at the root `teams/` checkout. It does not own Brain, Account, Assistant-release, or egress-proxy responsibilities.

## Source organization

- `assistant/`, `chat/`, `egress/`, `inference/`, `install/`, `integrations/`, `action/`, and `storage/` are
  named Team responsibilities.
- `core/` contains only cohesive Team invariants: canonical JSON, identifiers, and strict HTTP parsing and routing.
- `local/` owns the controller entrypoint, state, audit, validation, token, labels, and lifecycle used by the Local
  Space applied by the release-bound CLI. Its `assistant/`, `chat/`, `http/`, and `install/` children separate runtime
  responsibilities.
- `install/` owns publication verification and binding.
- `protocol/http/` is Team's HTTP authority; `protocol/assistant/` and `protocol/install/` are exact pinned mirrors
  used for independent admission and installation conformance.
- `egress/` owns Team policy and bindings; the enforcement proxy remains in the Assistant domain.
- The repository root contains governance and dependency metadata only; runtime Python belongs to a named
  responsibility or profile.

Directory names use the shortest clear responsibility term, such as `install/`. A peer domain may appear in a leaf
adapter name but never as a child domain owned by Team.

## Deletion contract

Team deletion is idempotent and fail-closed. A successful response includes `residue_absent`, naming every owned
state class proved absent: encrypted chat continuations, Brain and Action checkpoints, preparation helpers, Routines,
Assistant containers, egress policies, publication bindings, inference configuration, Team storage, the Team network
and name, integration credentials, Stored Inputs, and runtime state. A Local Space reset applies the same contract to
every owned Team.

## Local validation

Use Python 3.14 and the committed dependency lock. Lint with the umbrella's `ruff.toml` from the umbrella root
(`ruff check --config ruff.toml teams` and `ruff format --config ruff.toml --check teams`), then run the suite here:

```bash
uv run --frozen --python 3.14 python -m unittest discover -s tests -p "test_*.py"
```
