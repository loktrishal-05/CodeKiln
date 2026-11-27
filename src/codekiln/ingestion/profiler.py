"""Static repository profiler: detect stack, Python version, packaging, and test runner.

Trust boundary 1 (docs/SPEC.md): only static file inspection is performed; nothing from the
target repository is imported or executed.
"""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Stack = Literal["python", "node", "unknown"]
PackageManager = Literal["uv", "poetry", "pip", "none"]
TestRunner = Literal["pytest", "unittest", "unknown"]

SUPPORTED_PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13")
DEFAULT_PYTHON_VERSION = "3.12"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_SAFE_EXTRA_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_RELPATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_PY_VER_RE = re.compile(r"3\.(\d+)")
_DEV_EXTRAS = ("dev", "test", "tests", "testing")
_REQ_CANDIDATES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements/base.txt",
    "requirements/dev.txt",
    "requirements/test.txt",
)
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "dist", "build"}


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class RepoProfile:
    stack: Stack
    python_version: str
    package_manager: PackageManager
    test_runner: TestRunner
    test_command: tuple[str, ...]
    install_commands: tuple[str, ...]
    dependency_files: tuple[str, ...]
    extras: tuple[str, ...] = ()


def _safe_read_text(root: Path, rel_path: str) -> str | None:
    base = root.resolve()
    candidate = root / rel_path
    if not candidate.exists() and not candidate.is_symlink():
        return None
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise ProfileError(f"manifest symlink escapes repo: {rel_path}")
    if not resolved.is_file():
        return None
    if resolved.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ProfileError(f"manifest file exceeds size cap: {rel_path}")
    return resolved.read_text(encoding="utf-8", errors="replace")


def _has_files(root: Path, suffix: str) -> bool:
    for p in root.rglob(f"*{suffix}"):
        rel_parts = p.relative_to(root).parts
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        if p.is_file():
            return True
    return False


def _has_python_tests(root: Path) -> bool:
    for p in root.rglob("*.py"):
        rel_parts = p.relative_to(root).parts
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        name = p.name
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
            return True
    return False


def _pick_python_version(spec: str | None) -> str:
    if not spec:
        return DEFAULT_PYTHON_VERSION
    if m := re.search(r"==\s*3\.(\d+)", spec):
        ver = f"3.{m.group(1)}"
        if ver in SUPPORTED_PYTHON_VERSIONS:
            return ver
    if m := re.search(r"(?:>=|~=|\^)\s*3\.(\d+)", spec):
        minor = int(m.group(1))
        ver = f"3.{minor}"
        if ver in SUPPORTED_PYTHON_VERSIONS and minor >= 12:
            return ver
        if minor < 12 and "<3.12" not in spec.replace(" ", ""):
            return DEFAULT_PYTHON_VERSION
        if ver in SUPPORTED_PYTHON_VERSIONS:
            return ver
    if m := _PY_VER_RE.search(spec):
        ver = f"3.{m.group(1)}"
        if ver in SUPPORTED_PYTHON_VERSIONS:
            return ver
    return DEFAULT_PYTHON_VERSION


def _detect_python_version(root: Path, pyproject: dict[str, object]) -> str:
    py_ver_file = _safe_read_text(root, ".python-version")
    if py_ver_file:
        first_line = py_ver_file.strip().splitlines()[0].strip() if py_ver_file.strip() else ""
        if first_line:
            return _pick_python_version(first_line)
    project = pyproject.get("project")
    if isinstance(project, dict):
        req_py = project.get("requires-python")
        if isinstance(req_py, str):
            return _pick_python_version(req_py)
    tool = pyproject.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            deps = poetry.get("dependencies")
            if isinstance(deps, dict) and isinstance(deps.get("python"), str):
                return _pick_python_version(deps["python"])
    return DEFAULT_PYTHON_VERSION


def _detect_extras(pyproject: dict[str, object]) -> tuple[str, ...]:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return ()
    opt = project.get("optional-dependencies")
    if not isinstance(opt, dict):
        return ()
    found: list[str] = []
    for name in _DEV_EXTRAS:
        if name in opt and _SAFE_EXTRA_RE.fullmatch(name):
            found.append(name)
    return tuple(found)


def _pyproject_mentions_pytest(pyproject: dict[str, object]) -> bool:
    tool = pyproject.get("tool")
    if isinstance(tool, dict) and "pytest" in tool:
        return True
    project = pyproject.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        if isinstance(deps, list) and any("pytest" in str(d).lower() for d in deps):
            return True
        opt = project.get("optional-dependencies")
        if isinstance(opt, dict):
            for vals in opt.values():
                if isinstance(vals, list) and any("pytest" in str(d).lower() for d in vals):
                    return True
    return False


def profile_repo(root: Path) -> RepoProfile:
    """Statically inspect ROOT and return a validated RepoProfile."""
    if not root.exists() or not root.is_dir():
        raise ProfileError(f"repo directory does not exist: {root}")

    pyproject_raw = _safe_read_text(root, "pyproject.toml")
    pyproject: dict[str, object] = {}
    if pyproject_raw is not None:
        try:
            parsed = tomllib.loads(pyproject_raw)
            if isinstance(parsed, dict):
                pyproject = parsed
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"invalid pyproject.toml: {exc}") from exc

    setup_py = _safe_read_text(root, "setup.py")
    setup_cfg = _safe_read_text(root, "setup.cfg")
    req_files = [
        r for r in _REQ_CANDIDATES if _SAFE_RELPATH_RE.fullmatch(r) and _safe_read_text(root, r) is not None
    ]
    has_uv_lock = _safe_read_text(root, "uv.lock") is not None
    has_poetry_lock = _safe_read_text(root, "poetry.lock") is not None
    has_package_json = _safe_read_text(root, "package.json") is not None

    is_python = bool(
        pyproject_raw is not None
        or setup_py is not None
        or setup_cfg is not None
        or req_files
        or _has_files(root, ".py")
    )
    if not is_python:
        if has_package_json:
            return RepoProfile(
                stack="node",
                python_version=DEFAULT_PYTHON_VERSION,
                package_manager="none",
                test_runner="unknown",
                test_command=(),
                install_commands=(),
                dependency_files=("package.json",),
            )
        return RepoProfile(
            stack="unknown",
            python_version=DEFAULT_PYTHON_VERSION,
            package_manager="none",
            test_runner="unknown",
            test_command=(),
            install_commands=(),
            dependency_files=(),
        )

    dep_files: list[str] = []
    if pyproject_raw is not None:
        dep_files.append("pyproject.toml")
    if setup_cfg is not None:
        dep_files.append("setup.cfg")
    if setup_py is not None:
        dep_files.append("setup.py")
    if has_uv_lock:
        dep_files.append("uv.lock")
    if has_poetry_lock:
        dep_files.append("poetry.lock")
    dep_files.extend(req_files)

    tool = pyproject.get("tool")
    is_poetry = has_poetry_lock or (isinstance(tool, dict) and "poetry" in tool)
    if has_uv_lock:
        pkg_mgr: PackageManager = "uv"
    elif is_poetry:
        pkg_mgr = "poetry"
    elif pyproject_raw is not None or setup_py is not None or setup_cfg is not None or req_files:
        pkg_mgr = "pip"
    else:
        pkg_mgr = "none"

    extras = _detect_extras(pyproject)
    install_cmds: list[str] = []
    for req in req_files:
        install_cmds.append(f"python -m pip install --no-cache-dir -r {req}")
    if pyproject_raw is not None or setup_py is not None or setup_cfg is not None:
        target = f".[{','.join(extras)}]" if extras else "."
        install_cmds.append(f'python -m pip install --no-cache-dir "{target}"')

    pytest_ini = _safe_read_text(root, "pytest.ini")
    conftest = _safe_read_text(root, "conftest.py")
    req_mentions_pytest = any(
        "pytest" in (_safe_read_text(root, r) or "").lower() for r in req_files
    )
    uses_pytest = (
        _pyproject_mentions_pytest(pyproject)
        or pytest_ini is not None
        or conftest is not None
        or req_mentions_pytest
        or _has_python_tests(root)
    )
    runner: TestRunner = "pytest" if uses_pytest else "unknown"
    test_cmd: tuple[str, ...] = ("pytest", "-q") if runner == "pytest" else ()

    return RepoProfile(
        stack="python",
        python_version=_detect_python_version(root, pyproject),
        package_manager=pkg_mgr,
        test_runner=runner,
        test_command=test_cmd,
        install_commands=tuple(install_cmds),
        dependency_files=tuple(dep_files),
        extras=extras,
    )
