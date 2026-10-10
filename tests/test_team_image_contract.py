import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV_IMAGE = "ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded"
# The preparation helper's fixed worker runs from the same image as the controller (ADR-0093).
LOCAL_ENTRYPOINTS = ("local.app", "local.healthcheck", "prepare.worker")
ROOT_RUNTIME_DATA: set[str] = set()
PRODUCTION_PACKAGES = {
    "assistant",
    "chat",
    "core",
    "egress",
    "inference",
    "install",
    "integrations",
    "local",
    "action",
    "prepare",
    "routine",
    "storage",
}
# Package data has no import graph; this map is its reviewed necessity authority.
LOCAL_PACKAGE_DATA = {
    "inference": {"inference/model_catalog.json"},
    "install": set(),
}
PACKAGE_TOOLS: dict[str, set[str]] = {}
# The Developers Assistant protocol reference validators Team imports at run time (ADR-0091, ADR-0092).
ASSISTANT_PROTOCOL_RUNTIME = {
    "protocol/assistant/v1/validators/action_effect.py",
    "protocol/assistant/v1/validators/failure.py",
    "protocol/assistant/v1/validators/human_request.py",
    "protocol/assistant/v1/validators/input_file.py",
    "protocol/assistant/v1/validators/message_catalog.py",
    "protocol/assistant/v1/validators/route.py",
}
LOCAL_PROTOCOL_DATA = {
    *ASSISTANT_PROTOCOL_RUNTIME,
    "protocol/action/v1/schema.py",
    "protocol/http/v1/identifiers.py",
    "protocol/http/v1/payload.py",
    "protocol/http/v1/phrase.py",
    "protocol/http/v1/progress.py",
    "protocol/http/v1/purpose.py",
    "protocol/http/v1/routine.py",
    "protocol/http/v1/routine_context.py",
    "protocol/http/v1/routine_notice.py",
    "protocol/http/v1/routine_proposal.py",
    "protocol/http/v1/routine_run.py",
    "protocol/http/v1/strict_json.py",
    "protocol/http/v1/supervisor.py",
    "protocol/http/v1/turn.py",
    "protocol/install/upstream.json",
    "protocol/install/v1/README.md",
    "protocol/install/v1/contract-files.sha256",
    "protocol/install/v1/definitions.schema.json",
    "protocol/install/v1/resolve-response.schema.json",
    "protocol/install/v1/schema_validator.py",
    "protocol/install/v1/vectors.json",
    "protocol/install/v1/verify.py",
}
DYNAMIC_IMPORT_MODULES = {"importlib", "pkgutil", "runpy"}


def _source_imports(module: str, path: Path) -> set[str]:
    imports = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module
            if node.level:
                package_parts = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
                parent_levels = node.level - 1
                if parent_levels >= len(package_parts):
                    raise AssertionError(f"invalid relative import in {path}")
                base = package_parts[: len(package_parts) - parent_levels]
                imported_module = ".".join((*base, node.module) if node.module else base)
            if imported_module:
                imports.add(imported_module)
                imports.update(f"{imported_module}.{alias.name}" for alias in node.names)
    return imports


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _runtime_import_closure(*entrypoints: str) -> tuple[set[str], set[str], set[str]]:
    pending = [
        ".".join(parts[:depth])
        for entrypoint in entrypoints
        for parts in [entrypoint.split(".")]
        for depth in range(1, len(parts) + 1)
    ]
    visited = set()
    root_modules = set()
    root_packages = set()
    imported_paths = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = ROOT / f"{module.replace('.', '/')}.py"
        if not path.is_file():
            path = ROOT / module.replace(".", "/") / "__init__.py"
        if not path.is_file():
            continue
        imported_paths.add(path.relative_to(ROOT).as_posix())
        if "." not in module and path.parent == ROOT:
            root_modules.add(module)
        package = module.partition(".")[0]
        if path.parent != ROOT:
            if package == "protocol":
                for imported_module in _source_imports(module, path):
                    parts = imported_module.split(".")
                    pending.extend(".".join(parts[:depth]) for depth in range(1, len(parts) + 1))
                continue
            if package not in PRODUCTION_PACKAGES:
                raise AssertionError(f"{path} belongs to an unregistered production package")
            root_packages.add(package)
        for imported_module in _source_imports(module, path):
            parts = imported_module.split(".")
            pending.extend(".".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    return {f"{module}.py" for module in root_modules}, root_packages, imported_paths


def _copy_parts(line: str) -> tuple[list[str], str] | None:
    stripped = line.lstrip()
    if not stripped:
        return None
    instruction = stripped.split(maxsplit=1)[0].upper()
    if instruction == "ADD":
        raise AssertionError(f"ADD is outside the exact-closure contract: {line}")
    if instruction != "COPY":
        return None
    if not stripped.startswith("COPY "):
        raise AssertionError(f"COPY must use the modeled canonical spelling: {line}")
    parts = stripped.split()
    sources = parts[1:-1]
    unsupported_flags = {"--parents", "--exclude"}
    if any(flag.split("=", 1)[0] in unsupported_flags for flag in sources if flag.startswith("--")):
        raise AssertionError(f"unsupported COPY semantics: {line}")
    while sources and sources[0].startswith("--"):
        sources.pop(0)
    if any("*" in source or "?" in source or "[" in source for source in sources):
        raise AssertionError(f"wildcard COPY source is outside the exact-closure contract: {line}")
    return sources, parts[-1]


def _copied_package_sources(logical_lines: list[str]) -> dict[str, set[str]]:
    copied = {}
    for line in logical_lines:
        copy_parts = _copy_parts(line)
        if not copy_parts:
            continue
        sources, destination = copy_parts
        match = re.fullmatch(r"[.]\/([a-z][a-z0-9_]*)(?:/[a-z0-9_]+)*/", destination)
        if match and match.group(1) in PRODUCTION_PACKAGES:
            copied.setdefault(match.group(1), set()).update(sources)
    return copied


class StaticTeamImageContractTests(unittest.TestCase):
    def _assert_package_copy_closure(
        self,
        packages: set[str],
        copied_packages: dict[str, set[str]],
        imported_paths: set[str],
        package_data: dict[str, set[str]],
    ) -> None:
        self.assertEqual(set(copied_packages), packages)
        for package in packages:
            package_files = copied_packages[package]
            copied_python = {path for path in package_files if path.endswith(".py")}
            self.assertEqual(
                copied_python,
                {path for path in imported_paths if path.startswith(f"{package}/")} | PACKAGE_TOOLS.get(package, set()),
            )
            self.assertTrue(all(source.startswith(f"{package}/") for source in package_files))
            self.assertEqual(package_data.get(package, set()), package_files - copied_python)

    def _assert_image_closure(
        self,
        logical_lines: list[str],
        entrypoints: tuple[str, ...],
        package_data: dict[str, set[str]],
        protocol_data: set[str],
    ) -> None:
        root_copy_sources = self._root_copy_sources(logical_lines)
        packaged = {source for source in root_copy_sources if re.fullmatch(r"[a-z][a-z0-9_]*[.]py", source)}
        modules, packages, imported_paths = _runtime_import_closure(*entrypoints)
        self.assertEqual(packaged, modules)
        self.assertEqual(root_copy_sources, modules | ROOT_RUNTIME_DATA)
        modeled_destinations = {
            "./",
            "/opt/venv",
            "/usr/local/bin/cosign",
            "./protocol/action/v1/",
            "./protocol/assistant/v1/validators/",
            "./protocol/http/v1/",
            "./protocol/install/",
            "./protocol/install/v1/",
        }
        copied_protocol = set()
        for line in logical_lines:
            copy_parts = _copy_parts(line)
            if copy_parts:
                sources, destination = copy_parts
                if destination.startswith("./protocol/"):
                    copied_protocol.update(sources)
                package_destination = re.fullmatch(
                    r"[.]\/([a-z][a-z0-9_]*)(?:/[a-z0-9_]+)*/",
                    destination,
                )
                self.assertTrue(
                    destination in modeled_destinations
                    or (package_destination and package_destination.group(1) in PRODUCTION_PACKAGES),
                    f"unmodeled COPY destination: {line}",
                )
        self.assertEqual(copied_protocol, protocol_data)
        self._assert_package_copy_closure(
            packages,
            _copied_package_sources(logical_lines),
            imported_paths,
            package_data,
        )

    def _root_copy_sources(self, logical_lines: list[str]) -> set[str]:
        return {
            source
            for line in logical_lines
            if (copy_parts := _copy_parts(line)) and copy_parts[1] == "./"
            for source in copy_parts[0]
        }

    def test_static_build_context_excludes_dependencies_caches_and_secrets(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        self.assertLessEqual(
            {
                ".env",
                ".env.*",
                "**/.env",
                "**/.env.*",
                ".venv",
                "**/__pycache__",
                "**/*.pyc",
            },
            set(dockerignore),
        )

    def _assert_epoch_free_dependency_base(self, dockerfile: str) -> None:
        """The runtime derives from a dependency layer that no commit-time input reaches (Shimpz ADR-0098)."""
        self.assertIn("\nFROM dependencies AS runtime\n", dockerfile)
        stages = dict(re.findall(r"(?ms)^FROM \S+ AS (\w+)\n(.*?)(?=^FROM |\Z)", dockerfile))
        self.assertEqual(["cosign", "dependencies", "runtime", "uv"], sorted(stages))
        dependencies = re.sub(r"\\\n\s*", " ", stages["dependencies"])
        for stage in ("uv", "cosign", "dependencies"):
            with self.subTest(stage=stage):
                self.assertNotRegex(stages[stage], r"(?m)^(ARG SOURCE_DATE_EPOCH|WORKDIR|COPY|ADD)\b")
        for mount in (
            "--mount=type=tmpfs,target=/tmp",
            "--mount=type=bind,from=uv,source=/uv,target=/tmp/uv",
            "--mount=type=bind,source=pyproject.toml,target=/tmp/project/pyproject.toml",
            "--mount=type=bind,source=uv.lock,target=/tmp/project/uv.lock",
            "--mount=type=bind,from=cosign,source=/tmp/cosign,target=/tmp/cosign",
        ):
            self.assertIn(mount, dependencies)
        self.assertIn("uv sync --frozen --no-install-project --no-dev --python 3.14", dependencies)
        self.assertIn('echo "${cosign_sha256}  /tmp/cosign" | sha256sum -c -', stages["cosign"])
        self.assertTrue(dependencies.rstrip().endswith("find /opt -depth -exec touch -h -d @0 {} +"))
        # The base ships no bytecode; the standard library, environment, and application are compiled once.
        self.assertIn(
            "compileall -q -f --invalidation-mode checked-hash /usr/local/lib/python3.14 /opt/venv", dependencies
        )
        self.assertIn("compileall -q -f --invalidation-mode checked-hash /app\n", stages["runtime"])

    def test_static_local_image_copies_the_exact_runtime_import_closure(self) -> None:
        dockerfile = (ROOT / "local" / "Dockerfile").read_text(encoding="utf-8")
        for line in re.sub(r"\\\n\s*", " ", dockerfile).splitlines():
            _copy_parts(line)
        runtime = dockerfile.split(" AS runtime\n", 1)[1]
        logical_lines = re.sub(r"\\\n\s*", " ", runtime).splitlines()

        self.assertIn(f"FROM {UV_IMAGE} AS uv", dockerfile)
        self._assert_epoch_free_dependency_base(dockerfile)
        healthcheck = next(line for line in logical_lines if line.startswith("HEALTHCHECK "))
        self.assertEqual(
            " ".join(healthcheck.split()),
            "HEALTHCHECK --interval=30s --timeout=4s --start-period=30s --start-interval=1s --retries=3 "
            f'CMD ["/opt/venv/bin/python", "-m", "{LOCAL_ENTRYPOINTS[1]}"]',
        )
        self.assertIn(
            f'ENTRYPOINT ["/opt/venv/bin/python", "-m", "{LOCAL_ENTRYPOINTS[0]}"]',
            logical_lines,
        )
        self._assert_image_closure(
            logical_lines,
            LOCAL_ENTRYPOINTS,
            LOCAL_PACKAGE_DATA,
            LOCAL_PROTOCOL_DATA,
        )
        self.assertIn("/var/lib/shimpz-local/chat-continuations/state", runtime)
        self.assertIn("/var/lib/shimpz-local/chat-continuations/key", runtime)
        # A managed-disk Space populates each fresh volume from its image directory, owner and mode included.
        self.assertIn("/var/lib/shimpz-local/routines/state /var/lib/shimpz-local/routines/key", runtime)
        self.assertIn("groupadd --gid 10021 shimpzsupervisor-key", runtime)
        self.assertIn("chmod 2770 /run/shimpz-local-supervisor", runtime)
        self.assertNotIn("apt-get", runtime)
        self.assertNotIn("curl", runtime)
        self.assertNotIn("/usr/local/bin/uv", runtime)
        protocol = ROOT / "protocol" / "install"
        self.assertEqual({"upstream.json", "v1"}, {path.name for path in protocol.iterdir()})
        for package in PRODUCTION_PACKAGES:
            package_tree = ast.parse((ROOT / package / "__init__.py").read_text(encoding="utf-8"))
            self.assertFalse(
                [node for node in ast.walk(package_tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
            )

    def test_every_package_module_is_reachable_from_an_image_entrypoint(self) -> None:
        local_modules, local_packages, local_paths = _runtime_import_closure(*LOCAL_ENTRYPOINTS)

        self.assertEqual({path.name for path in ROOT.glob("*.py")}, local_modules)
        self.assertEqual({path.name for path in ROOT.glob("*.json")}, ROOT_RUNTIME_DATA)
        filesystem_packages = {
            path.name for path in ROOT.iterdir() if path.is_dir() and (path / "__init__.py").is_file()
        }
        self.assertEqual(PRODUCTION_PACKAGES, filesystem_packages)
        self.assertEqual(PRODUCTION_PACKAGES, local_packages)
        for package in PRODUCTION_PACKAGES:
            package_files = {
                path.relative_to(ROOT).as_posix() for path in (ROOT / package).rglob("*.py")
            } - PACKAGE_TOOLS.get(package, set())
            self.assertEqual(
                package_files,
                {path for path in local_paths if path.startswith(f"{package}/")},
            )
        source_package_data = {
            path.relative_to(ROOT).as_posix()
            for package in PRODUCTION_PACKAGES
            for path in (ROOT / package).rglob("*")
            if path.is_file()
            and path.name != "Dockerfile"
            and path.suffix not in {".py", ".pyc"}
            and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(ROOT).parts)
        }
        declared_package_data = {path for paths in LOCAL_PACKAGE_DATA.values() for path in paths}
        self.assertEqual(source_package_data, declared_package_data)
        protocol_install_data = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "protocol" / "install").rglob("*")
            if path.is_file() and not any(part == "__pycache__" for part in path.relative_to(ROOT).parts)
        }
        local_protocol_runtime_data = {path for path in local_paths if path.startswith("protocol/")}
        self.assertEqual(
            local_protocol_runtime_data,
            {
                path
                for path in LOCAL_PROTOCOL_DATA
                if path.startswith(("protocol/action/", "protocol/http/", "protocol/assistant/"))
            },
        )
        self.assertEqual(protocol_install_data | local_protocol_runtime_data, LOCAL_PROTOCOL_DATA)
        production_sources = [*ROOT.glob("*.py")]
        production_sources.extend(path for package in PRODUCTION_PACKAGES for path in (ROOT / package).rglob("*.py"))
        for path in production_sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported_roots = {module.partition(".")[0] for module in _source_imports(_module_name(path), path)}
            self.assertEqual(
                set(),
                imported_roots & DYNAMIC_IMPORT_MODULES,
                f"{path.relative_to(ROOT)} imports a dynamic-loading module",
            )
            dynamic_imports = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Name)
                        and node.func.id in {"__import__", "compile", "eval", "exec", "import_module"}
                    )
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "__import__")
                )
            ]
            self.assertEqual([], dynamic_imports, f"{path.relative_to(ROOT)} hides imports from the image closure")

    def test_reference_image_exposes_the_sdk_baked_manifest_contract(self) -> None:
        dockerfile = (ROOT / "tests" / "fixtures" / "reference-assistant" / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("tests/fixtures/reference-assistant/shimpz.toml /opt/shimpz/shimpz.toml", dockerfile)
        self.assertIn(
            "tests/fixtures/reference-assistant/shimpz.contract.json /opt/shimpz/shimpz.contract.json",
            dockerfile,
        )
        self.assertNotIn("assistant_catalog", dockerfile)
        self.assertIn("/opt/shimpz/shimpz.toml /opt/shimpz/shimpz.contract.json", dockerfile)


if __name__ == "__main__":
    unittest.main()
