# Teams repository rules

- This repository owns the Team domain. It does not own Developers, Brain, Account, Assistant release, or egress
  policy merely because it integrates with them.
- Organize profile-specific source under `hosted/` or `local/`. Keep source at the repository root only while its
  ownership is genuinely profile-neutral and its final responsibility has been classified.
- Name responsibility directories with the shortest clear term. Use `install/`, not `installation/`.
- Place peer integrations beneath the Team responsibility they serve. A peer may appear in a leaf adapter name such
  as `developers_client.py`; do not create a peer-domain directory that implies Team owns that domain.
- Use `protocol/` only for versioned wire schemas, vectors, semantic validation, and integrity evidence. It is not
  executable Team core logic.
- Do not create `core/`, `shared/`, `common/`, `utils/`, or `misc/` as a convenience. `core/` is allowed only for
  cohesive Team invariants proven to be shared by Hosted and Local.
- Preserve Team isolation, least privilege, fail-closed validation, secret redaction, and exact image-copy closure
  while moving source.
- Use Python 3.14. Run Ruff from the umbrella root with `ruff check --config ruff.toml teams`; run the complete local
  suite with `uv run --project teams --frozen --python 3.14 python -m unittest discover -s teams/tests`.
