# Shimpz Teams

`shimpz-teams` owns the Team domain: Team and Assistant authority, isolation, authorization, lifecycle, and the
Docker-mediated workload boundary.

The repository is consumed by the Shimpz umbrella at the root `teams/` checkout. It does not own Brain, Account, Assistant-release, or egress-proxy responsibilities.

## Source organization

- The Team controller is one Rust workspace (ADR-0123): each crate lives in the responsibility directory it serves.
  `action/`, `assistant/`, `browser/task/`, `chat/`, `egress/`, `inference/`, `install/`, `integrations/`,
  `prepare/`, `routine/`, and `storage/` are named Team responsibilities.
- `core/` contains only cohesive Team invariants: the clock, entropy, file custody, durable writes, and identifiers.
- `local/` is the `shimpz-team` binary and the only crate that reaches Docker or serves HTTP: the composition root,
  the HTTP server and authority, Docker and isolation, lifecycle coordination, audit, and each service group used by
  the Local Space applied by the release-bound CLI.
- `install/` owns publication verification and binding.
- `protocol/http/` is Team's HTTP authority: the Rust crate `rust/` implements it, and `v1/` holds its specification
  and vectors; `protocol/assistant/` and `protocol/install/` are exact pinned mirrors
  used for independent admission and installation conformance.
- `egress/` owns Team policy and bindings; the enforcement proxy remains in the Assistant domain.
- The repository root contains governance and the workspace's dependency metadata only; every crate belongs to a
  named responsibility or profile.

Directory names use the shortest clear responsibility term, such as `install/`. A peer domain may appear in a leaf
adapter name but never as a child domain owned by Team.

## Deletion contract

Team deletion is idempotent and fail-closed. A successful response includes `residue_absent`, naming every owned
state class proved absent: encrypted chat continuations, Brain and Action checkpoints, preparation helpers, Routines,
Assistant containers, egress policies, publication bindings, inference configuration, Team storage, the Team network
and name, integration credentials, Stored Inputs, and runtime state. A Local Space reset applies the same contract to
every owned Team.

## Local validation

From the umbrella root, run the workspace's lane (fmt, clippy, documentation, machete, the tests, and the fuzz
targets): `.scripts/gate/rust crate teams`. The real-daemon controller suite runs from the `fixture` target of
`local/Dockerfile` through `.scripts/gate/live controller-local`.
