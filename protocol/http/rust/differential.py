"""Compare the Rust crate with the Python modules of teams/protocol/http/v1 on seeded cases (tooling).

Run with Team's interpreter and isolation, from anywhere:

    teams/.venv/bin/python -I teams/protocol/http/rust/differential.py --count 1000000 [--only NAME ...]

For every registry entry (`oracle/`, the same Python names as `tests/protocol/support/registry/`), it draws `--count`
cases from `--seed`: each a seed value from that entry's vectors, changed by one to three random edits of the generic
mutator below. The Python module decides each case after a strict JSON round trip; the release `decide` example decides
the same bytes; any line that differs is a disagreement, printed with its case. It prints, per entry, the cases, how
many the Python side answered with a value, refused, or raised, the disagreements, and the seconds it took, and exits
non-zero on any disagreement. Its pool and its cargo build use SHIMPZ_TEST_WORKERS, else half of the processors.

The generator is a seeded SplitMix64, as in .standards/strict-json, so a seed names the same cases on every Python
version. This file and `oracle/` are tooling, deleted with the Python modules (plan phase R9).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import struct
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
V1_PREFIX = str(ROOT.parent / "v1") + "/"
sys.path[:0] = [str(ROOT.parent / "v1"), str(ROOT)]

oracle = importlib.import_module("oracle")
ENTRIES, outcome_line = oracle.ENTRIES, oracle.outcome_line
SplitMix64, mutate = oracle.mutate.SplitMix64, oracle.mutate.mutate

SEED = 20261029
CHUNK = 20_000
CORPUS_CASE_BYTES = 2048
# An entry with at most this many seeds keeps every one; a larger generated seed set keeps those that add an arc.
CORPUS_SEEDS = 400
SHOWN = 5


def workers() -> int:
    configured = os.environ.get("SHIMPZ_TEST_WORKERS")
    if configured:
        if not configured.isdigit() or int(configured) < 1:
            raise SystemExit("SHIMPZ_TEST_WORKERS must be a positive integer")
        return int(configured)
    processors = os.process_cpu_count() or 1
    return processors if os.environ.get("GITHUB_ACTIONS") else max(1, processors // 2)


def build_decide(jobs: int) -> Path:
    subprocess.run(
        ["cargo", "build", "--quiet", "--release", "--locked", "--jobs", str(jobs), "--example", "decide"],
        cwd=ROOT,
        check=True,
    )
    metadata = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps", "--locked"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return Path(json.loads(metadata.stdout)["target_directory"]) / "release" / "examples" / "decide"


def chunk_cases(task: tuple[str, int, int, int]) -> list[tuple[bytes, str]]:
    """The cases of one chunk of one entry, each as JSON bytes with the Python line."""
    name, seed, index, count = task
    call, seeds = ENTRIES[name]
    pool = seeds()
    rng = SplitMix64(seed ^ (hash_name(name) << 20) ^ index)
    cases = []
    for _ in range(count):
        value = pool[rng.below(len(pool))]
        for _ in range(rng.below(4)):
            value = mutate(rng, value)
        data = json.dumps(value, ensure_ascii=False).encode()
        cases.append((data, outcome_line(call, data)))
    return cases


def hash_name(name: str) -> int:
    """A stable 32-bit hash of an entry name (FNV-1a), never Python's salted hash."""
    value = 0x811C9DC5
    for byte in name.encode():
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def run_rust(binary: Path, name: str, cases: list[tuple[bytes, str]]) -> list[str]:
    encoded = name.encode()
    framed = b"".join(
        struct.pack(">I", len(encoded)) + encoded + struct.pack(">I", len(data)) + data for data, _ in cases
    )
    completed = subprocess.run([str(binary)], input=framed, capture_output=True, check=True)
    return completed.stdout.decode().split("\n")[:-1]


def compare(binary: Path, pool: ProcessPoolExecutor, name: str, count: int, seed: int) -> tuple[int, dict[str, int]]:
    tasks = [(name, seed, index, min(CHUNK, count - start)) for index, start in enumerate(range(0, count, CHUNK))]
    disagreements = 0
    tally = {"value": 0, "refuse": 0, "error": 0}
    for cases in pool.map(chunk_cases, tasks):
        actual = run_rust(binary, name, cases)
        if len(actual) != len(cases):
            raise SystemExit(f"{name}: decide wrote {len(actual)} lines for {len(cases)} cases")
        for (data, expected), line in zip(cases, actual, strict=True):
            kind = (
                "refuse" if expected == "refuse" else "error" if expected.startswith(("error:", "raise:")) else "value"
            )
            tally[kind] += 1
            if expected != line:
                disagreements += 1
                if disagreements <= SHOWN:
                    print(f"{name} case {data[:300]!r}", file=sys.stderr)
                    print(f"  python {expected[:300]}\n  rust   {line[:300]}", file=sys.stderr)
    return disagreements, tally


class ArcTracer:
    """Records the line-to-line arcs the Python modules execute, so the corpus keeps a case only when it adds one."""

    def __init__(self) -> None:
        self.seen: set[tuple[str, int, int]] = set()
        self.fresh = False
        self.previous: dict[object, int] = {}

    def trace(self, frame, event, _argument):
        filename = frame.f_code.co_filename
        if not filename.startswith(V1_PREFIX):
            return None
        if event == "line":
            arc = (filename, self.previous.get(frame, -frame.f_code.co_firstlineno), frame.f_lineno)
            if arc not in self.seen:
                self.seen.add(arc)
                self.fresh = True
            self.previous[frame] = frame.f_lineno
        return self.trace

    def adds(self, call, data: bytes) -> tuple[bool, str]:
        self.fresh = False
        self.previous.clear()
        sys.settrace(self.trace)
        try:
            line = outcome_line(call, data)
        finally:
            sys.settrace(None)
        return self.fresh, line


def write_corpus(directory: Path, names: list[str], draws: int, seed: int) -> None:
    """Write, per module, each entry's seeds and every drawn case that executes a new line arc of the Python modules.

    An entry with more than CORPUS_SEEDS seeds keeps only the seeds that execute a new arc too.

    Each module's cases go to `<directory>/<module>.json` with their Python lines; tests/protocol/corpus.rs replays
    every file there. Only the modules of `names` are rewritten.
    """
    tracer = ArcTracer()
    modules: dict[str, list[dict[str, str]]] = {}
    for name in names:
        call, seeds = ENTRIES[name]
        unmutated = [json.dumps(value, ensure_ascii=False).encode() for value in seeds()]
        drawn = [data for data, _ in chunk_cases((name, seed, 0, draws))]
        cases = modules.setdefault(name.split(".")[0], [])
        for index, data in enumerate([*unmutated, *drawn]):
            fresh, line = tracer.adds(call, data)
            if (index < len(unmutated) and len(unmutated) <= CORPUS_SEEDS) or (
                fresh and len(data) <= CORPUS_CASE_BYTES
            ):
                cases.append({"name": name, "input": data.decode(), "line": line})
    directory.mkdir(exist_ok=True)
    for module, cases in modules.items():
        rows = ",\n".join(json.dumps(case, ensure_ascii=True) for case in cases)
        (directory / f"{module}.json").write_text(f"[\n{rows}\n]\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--only", nargs="*", default=None, help="these entry names")
    parser.add_argument("--modules", nargs="*", default=None, help="every entry of these modules (`payload`, ...)")
    parser.add_argument(
        "--corpus", type=int, metavar="DRAWS", help="write tests/protocol/corpus/ from this many draws and stop"
    )
    arguments = parser.parse_args()
    names = arguments.only or [
        name for name in ENTRIES if arguments.modules is None or name.split(".")[0] in arguments.modules
    ]
    unknown = set(names) - set(ENTRIES)
    if unknown:
        raise SystemExit(f"unknown entries: {sorted(unknown)}")
    if arguments.corpus:
        write_corpus(ROOT / "tests" / "protocol" / "corpus", names, arguments.corpus, arguments.seed)
        return 0
    jobs = workers()
    binary = build_decide(jobs)
    total = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for name in names:
            started = time.monotonic()
            disagreements, tally = compare(binary, pool, name, arguments.count, arguments.seed)
            total += disagreements
            print(
                f"{name}: {arguments.count} cases ({tally['value']} values, {tally['refuse']} refused, "
                f"{tally['error']} errors), {disagreements} disagreements, {time.monotonic() - started:.1f} s",
                flush=True,
            )
    print(f"seed {arguments.seed}: {len(names)} entries, {total} disagreements")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
