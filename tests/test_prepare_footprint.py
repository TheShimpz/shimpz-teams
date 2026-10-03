"""The controller never imports the image or PDF parsers; only the helper's worker does (ADR-0093)."""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARSERS = frozenset({"PIL", "pypdf"})
PARSER_MODULES = frozenset({"prepare.image", "prepare.pdf"})


def _module_path(module: str) -> Path | None:
    for candidate in (ROOT / f"{module.replace('.', '/')}.py", ROOT / module.replace(".", "/") / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _top_level_imports(module: str, path: Path) -> set[str]:
    """Imports executed when the module itself is imported: module scope only, never inside a function."""
    names: set[str] = set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(
                    package.split(".")[: len(package.split(".")) - node.level + 1] + ([base] if base else [])
                )
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _import_closure(*entrypoints: str) -> set[str]:
    pending = list(entrypoints)
    seen: set[str] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(module)
        if path is None:
            continue
        for name in _top_level_imports(module, path):
            parts = name.split(".")
            pending.extend(".".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    return seen


class ControllerFootprintTests(unittest.TestCase):
    def test_controller_entrypoints_never_import_the_parsers(self) -> None:
        for entrypoint in ("local.app", "hosted.app", "local.healthcheck", "hosted.healthcheck"):
            with self.subTest(entrypoint=entrypoint):
                closure = _import_closure(entrypoint)
                self.assertFalse({name.split(".")[0] for name in closure} & PARSERS)
                self.assertFalse(closure & PARSER_MODULES)
        # The worker is the one entrypoint that reaches them, and only lazily.
        self.assertFalse({name.split(".")[0] for name in _import_closure("prepare.worker")} & PARSERS)

    def test_importing_the_local_controller_loads_no_parser(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "tests.prepare_footprint_probe"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
            timeout=60,
        )
        self.assertEqual(completed.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
