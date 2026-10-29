"""Compare the Rust crate with the Python oracle on seeded documents (tooling; run with `python3 -I`).

Generates `--count` documents from `--seed` with the corpus generator of `vectors.py`, decides each with the oracle,
runs the release `decide` example once over all of them, and exits non-zero on the first disagreement it prints. Its
oracle pool and its cargo build use the gate's worker budget (`.scripts/gate/budget.py workers`).
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import os
import struct
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHOWN = 10


def load_vectors():
    spec = importlib.util.spec_from_file_location("strict_json_vectors", ROOT / "vectors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VECTORS = load_vectors()


def oracle_lines(documents: list[bytes]) -> list[bytes]:
    return [(VECTORS.decide(document) or "refuse").encode() for document in documents]


def workers() -> int:
    """SHIMPZ_TEST_WORKERS when set, else half of this process's processors locally and all of them under Actions."""
    configured = os.environ.get("SHIMPZ_TEST_WORKERS")
    if configured:
        if not configured.isdigit() or int(configured) < 1:
            raise SystemExit("SHIMPZ_TEST_WORKERS must be a positive integer")
        return int(configured)
    processors = os.process_cpu_count() or 1
    return processors if os.environ.get("GITHUB_ACTIONS") else max(1, processors // 2)


def run_rust(documents: list[bytes], jobs: int) -> list[bytes]:
    framed = b"".join(struct.pack(">I", len(document)) + document for document in documents)
    completed = subprocess.run(
        ["cargo", "run", "--quiet", "--release", "--locked", "--jobs", str(jobs), "--example", "decide"],
        cwd=ROOT,
        input=framed,
        capture_output=True,
        check=True,
    )
    return completed.stdout.split(b"\n")[:-1]


def decide_all(documents: list[bytes], workers: int) -> list[bytes]:
    chunk = max(1, len(documents) // (workers * 8))
    chunks = [documents[start : start + chunk] for start in range(0, len(documents), chunk)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return [line for lines in pool.map(oracle_lines, chunks) for line in lines]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=VECTORS.SEED)
    arguments = parser.parse_args()
    rng = VECTORS.SplitMix64(arguments.seed)
    documents = [VECTORS.document(rng) for _ in range(arguments.count)]
    jobs = workers()
    expected = decide_all(documents, jobs)
    actual = run_rust(documents, jobs)
    if len(actual) != len(expected):
        print(f"decide wrote {len(actual)} lines for {len(expected)} documents", file=sys.stderr)
        return 1
    mismatches = [index for index, pair in enumerate(zip(expected, actual, strict=True)) if pair[0] != pair[1]]
    for index in mismatches[:SHOWN]:
        print(f"document {index} {base64.b64encode(documents[index]).decode()}", file=sys.stderr)
        print(f"  python {expected[index][:200]!r}\n  rust   {actual[index][:200]!r}", file=sys.stderr)
    accepted = sum(line != b"refuse" for line in expected)
    print(
        f"seed {arguments.seed}: {len(documents)} documents, {accepted} accepted, "
        f"{len(documents) - accepted} refused, {len(mismatches)} disagreements"
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
