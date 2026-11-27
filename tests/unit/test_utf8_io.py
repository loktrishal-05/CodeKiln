"""Found by the first real run on Windows: a model's text had U+2011 (a non-breaking hyphen), which the
cp1252 default encoding cannot store, so a valid spec crashed while the package was written.

CI is Linux (UTF-8 by default) and cannot see this, so the guard is static: no read_text/write_text in
src may rely on the platform's default encoding.
"""
import ast
from pathlib import Path

from spec_examples import REPO, SHA, make_spec

from codekiln.synthesis.emit import write_package

SRC = Path(__file__).resolve().parents[2] / "src"


def test_no_write_in_src_relies_on_the_platform_newline() -> None:
    """On Windows write_text turns every LF into CRLF: a CRLF patch is "corrupt" to git, and CRLF in
    test.sh / solve.sh / the Dockerfile breaks them inside the Linux containers Harbor builds."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_text"
                    and not any(k.arg == "newline" for k in node.keywords)):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, "write_text without newline=: " + ", ".join(offenders)


def test_a_built_package_has_no_crlf_in_any_file(tmp_path: Path) -> None:
    package = write_package(tmp_path / "task", make_spec(), repo_url=REPO, base_commit=SHA)
    crlf = [str(p.relative_to(package)) for p in package.rglob("*") if p.is_file() and b"\r\n" in p.read_bytes()]
    assert crlf == []  # the reference patch, tests and scripts must be byte-for-byte LF


def test_no_file_io_in_src_relies_on_the_platform_default_encoding() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"read_text", "write_text"}
                    and not any(k.arg == "encoding" for k in node.keywords)):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, "read_text/write_text without encoding=: " + ", ".join(offenders)


def test_a_package_with_characters_cp1252_cannot_store_round_trips_as_utf8(tmp_path: Path) -> None:
    tricky = "non‑breaking hyphen, em dash —, arrows →, accents é, CJK 漢"
    spec = make_spec(instruction=make_spec().instruction.replace("Uppercase echo", f"Uppercase echo ({tricky})"))
    package = write_package(tmp_path / "task", spec, repo_url=REPO, base_commit=SHA)
    on_disk = (package / "instruction.md").read_bytes().decode("utf-8")  # the bytes really are UTF-8
    assert tricky in on_disk
    assert tricky in (package / "instruction.md").read_text(encoding="utf-8")
    assert (package / "rtm_map.json").read_bytes().decode("utf-8")  # every other written file decodes too
