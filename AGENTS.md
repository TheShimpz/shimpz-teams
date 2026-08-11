# Teams repository rules

## Authority

- This repository owns the Team domain. It does not own Developers, Brain, Account, Assistant release, or the
  Assistant egress proxy merely because it integrates with them.
- Team owns its outbound policy and bindings under `egress/`; `assistants/egress` owns the separate enforcement
  proxy. Do not merge policy authority and network enforcement into one component.
- Organize profile-specific source under `hosted/` or `local/`. Keep source at the repository root only while its
  ownership is genuinely profile-neutral and its final responsibility has been classified.
- Name responsibility directories with the shortest clear term. Use `install/`, not `installation/`.
- Place peer integrations beneath the Team responsibility they serve. A peer may appear in a leaf adapter name such
  as `developers_client.py`; do not create a peer-domain directory that implies Team owns that domain.
- Use `protocol/` only for versioned wire schemas, vectors, semantic validation, and integrity evidence. It is not
  executable Team core logic.
- Team owns its HTTP protocol. The neutral Assistant-install protocol is a generated, pinned mirror; Developers
  owns the published Assistant specification while Team admission remains independently fail-closed.
- Do not create `core/`, `shared/`, `common/`, `utils/`, or `misc/` as a convenience. `core/` is allowed only for
  cohesive Team invariants proven to be shared by Hosted and Local.
- Prefer a named profile-neutral responsibility such as `chat/`, `egress/`, `inference/`, or `action/` over hiding
  that responsibility in `core/`.
- Preserve Team isolation, least privilege, fail-closed validation, secret redaction, and exact image-copy closure
  while moving source.

## Shared delivery

- Read the canonical [Shimpz architecture](https://github.com/TheShimpz/shimpz/blob/main/.context/ARCHITECTURE.md)
  before changing vocabulary, cross-domain authority, protocols, topology, or placement.
- Deliver the smallest useful microtask, validate it, commit it with a clear English conventional message, and
  push it immediately.
- When working through the umbrella checkout, commit and push this repository before committing its umbrella
  gitlink.
- Shimpz is pre-production. Change the current contract directly; do not add aliases, dual imports, fallback
  parsers, or cleanup code triggered only by retired repository state.
- Tests that support workers use half of local processors and all GitHub Actions runner processors.

## Validation

- Use Python 3.14. Run `ruff check --config ruff.toml .`; run the complete local suite with
  `uv run --frozen --python 3.14 python -m unittest discover -s tests`.
