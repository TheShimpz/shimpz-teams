# Shimpz Teams

`shimpz-teams` owns the Team domain: Team and Assistant authority, isolation, authorization, lifecycle, and the
Docker-mediated workload boundary.

The repository is consumed by the Shimpz umbrella at the root `teams/` checkout. It is not a Service repository and
does not own Brain, Account, Assistant-release, or egress-policy responsibilities.

## Local validation

Use Python 3.14 and the committed dependency lock:

```bash
ruff check --config ruff.toml .
uv run --frozen --python 3.14 python -m unittest discover -s tests -p "test_*.py"
```
