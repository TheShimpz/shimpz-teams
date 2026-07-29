# Shimpz Teams

`shimpz-teams` owns the Team domain: Team and Assistant authority, isolation, authorization, lifecycle, and the
Docker-mediated workload boundary.

The repository is consumed by the Shimpz umbrella at the root `teams/` checkout. It is not a Service repository and
does not own Brain, Account, Assistant-release, or egress-policy responsibilities.

## Source organization

- `hosted/` contains the Hosted entrypoint, healthcheck, and Hosted-only responsibilities.
- `hosted/install/` owns the Team side of resolving, authorizing, binding, and materializing a published Assistant.
- `hosted/install/protocol/` is the verified wire-protocol mirror consumed by that responsibility.
- `local/` contains the Local entrypoint, healthcheck, and image definition used by `install.shimpz.com`.
- Root packages are existing profile-neutral candidates. They move to `core/` only after their contents prove one
  cohesive Team responsibility shared by both profiles.

Directory names use the shortest clear responsibility term, such as `install/`. A peer domain may appear in a leaf
adapter name but never as a child domain owned by Team.

## Local validation

Use Python 3.14 and the committed dependency lock:

```bash
ruff check --config ruff.toml .
uv run --frozen --python 3.14 python -m unittest discover -s tests -p "test_*.py"
```
