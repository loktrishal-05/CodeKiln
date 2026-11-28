"""Static HTML firing report: one self-contained file a judge can open offline (no JS, no CDN).

Reads the SQLite ledger for RUN_ID, plus (optional) the packaged tasks dir and the cross-task
`variation_matrix.json`. Everything shown traces back to a file or a ledger row. Repo-derived text
(instructions, rejection reasons) is untrusted, so every value goes through html.escape.
"""
import itertools
import json
import sqlite3
import tomllib
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Any

from codekiln.packaging.packager import verify_manifest
from codekiln.states import PIPELINE, State

# Incandescence scale for the matrix: cold firebrick -> white heat (reward 0.0 -> 1.0).
_HEAT = [(0.0, (0x2B, 0x2F, 0x38)), (0.25, (0x7A, 0x24, 0x14)), (0.5, (0xC2, 0x4A, 0x0E)),
         (0.75, (0xF0, 0xA0, 0x20)), (1.0, (0xFF, 0xF6, 0xDC))]


def heat(r: float) -> tuple[str, str]:
    """Background and readable text colour for a reward in [0, 1]."""
    r = min(max(r, 0.0), 1.0)
    rgb: tuple[int, ...] = _HEAT[-1][1]
    for (a, ca), (b, cb) in itertools.pairwise(_HEAT):
        if r <= b:
            t = (r - a) / (b - a)
            rgb = tuple(round(x + (y - x) * t) for x, y in zip(ca, cb, strict=True))
            break
    luminance = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    return "#" + "".join(f"{c:02x}" for c in rgb), "#1b2330" if luminance > 140 else "#f4f1ec"


def _e(v: object) -> str:
    return escape("" if v is None else str(v))


def _fmt(r: float | None) -> str:
    return "crash" if r is None else f"{r:.2f}"


def _mapping(value: object) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _rows(con: sqlite3.Connection, sql: str, *args: object) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args)]


def _empty(what: str, cmd: str) -> str:
    return f'<p class="empty">{what} Run <code>{_e(cmd)}</code> to fill this in.</p>'


def _funnel(cands: list[dict[str, Any]]) -> str:
    if not cands:
        return _empty("No candidates in this run yet.", "codekiln run <repo>")
    order = {s.value: i for i, s in enumerate(PIPELINE)}
    reached = [0] * len(PIPELINE)
    for c in cands:
        if c["status"] == State.REJECTED.value:
            # "STATE_B: ..." names the gate that rejected it; every stage before that was reached
            gate = (c["rejection_reason"] or "").split(":")[0].strip().upper()  # "mutation:" names MUTATION_OK
            stop = next((i for s, i in order.items() if gate and s.startswith(gate)),
                        order[State.DIVERSITY_OK.value] + 1)
        else:
            stop = order.get(c["status"], 0) + 1
        for i in range(stop):
            reached[i] += 1
    n = len(cands)
    # one row per drop: keep a stage only if fewer candidates got past the next one (and PACKAGED)
    keep = [i for i in range(len(PIPELINE)) if i == len(PIPELINE) - 1 or reached[i + 1] != reached[i]]
    bars = "".join(
        f'<li><span class="stage">{_e(s.value.replace("_", " ").lower())}</span>'
        f'<span class="bar" style="--w:{100 * k / n:.1f}%"></span><span class="num">{k}</span></li>'
        for s, k in ((PIPELINE[i], reached[i]) for i in keep)
    )
    return f'<ol class="funnel" aria-label="Candidates reaching each stage">{bars}</ol>'


def _matrix(m: dict[str, Any] | None) -> str:
    if not m:
        return _empty("No variation matrix yet.", "codekiln matrix packages/*")
    names = m["tasks"]
    head = "".join(f'<th scope="col"><span>{_e(n)}</span></th>' for n in names)
    body = ""
    for name, row in zip(names, m["reward"], strict=True):
        cells = ""
        for r in row:
            bg, fg = heat(r) if r is not None else ("#edeff1", "#a8321f")
            cells += f'<td style="background:{bg};color:{fg}">{_fmt(r)}</td>'
        body += f'<tr><th scope="row">{_e(name)}</th>{cells}</tr>'
    if m["distinct"]:
        verdict = (f'<p class="ok">All {len(names)} tasks are distinct: each reference solution passes '
                   "only its own tests.</p>")
    else:
        bits = [f"{_e(o['solution'])}'s solution scores {o['reward']:.2f} on {_e(o['tests'])}'s tests"
                for o in m["overlaps"]]
        bits += [f"{_e(b)} fails its own tests" for b in m["broken"]]
        bits += [f"{len(m['crashed'])} trial(s) crashed"] if m["crashed"] else []
        verdict = f'<p class="bad">Not distinct: {"; ".join(bits)}.</p>'
    return (f'{verdict}<div class="scroll"><table class="matrix"><caption>Rows: reference solution. '
            f"Columns: tests it was run against. Cell: Harbor oracle reward.</caption>"
            f"<thead><tr><td></td>{head}</tr></thead><tbody>{body}</tbody></table></div>")


def _logged(d: Path, agent: str) -> list[float] | None:
    """The Harbor reward of one `nop`/`oracle` trial kept beside a sealed package (`../harbor_logs/<name>/`).

    A sealed package cannot be edited (its manifest would break), so samples/ keeps its trial results next
    to it instead of inside it."""
    p = d.parent / "harbor_logs" / d.name / f"{agent}.json"
    try:
        return [float(json.loads(p.read_text(encoding="utf-8"))["reward"])]
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _checks(d: Path) -> list[tuple[str, bool | None]]:
    """The validity checklist of one package, each answer read from a file the pipeline wrote (None = unknown)."""
    p = _package(d)
    gates, m = _mapping(p["receipt"].get("gates")), p["metrics"]

    def load(name: str) -> dict[str, Any]:
        try:
            return _mapping(json.loads((d / name).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    rtm, audit, dossier = load("rtm_map.json"), load("security_audit.json"), load("mutation_dossier.json")
    probes = dossier.get("probes") or []
    fixed = [x for x in probes if x.get("kind") in ("nop", "stub", "partial")]
    kill, threshold = m["mutation_kill_rate"], dossier.get("threshold", 0.8)
    nop, oracle = m["nop_rewards"], m["oracle_rewards"]
    env = _mapping(tomllib.loads((d / "task.toml").read_text(encoding="utf-8")).get("environment"))
    return [
        ("Every requirement is mapped to a feature test", rtm.get("ok") if rtm else None),
        ("Feature tests fail without the solution and pass with it",
         (all(r == 0.0 for r in nop) and all(r == 1.0 for r in oracle)) if nop and oracle
         else (True if gates.get("state_c_all_pass") else None)),
        ("Existing tests are guarded (regression set recorded)", (p["p2p"] or 0) > 0 if p["p2p"] is not None else None),
        ("No network while the task runs", env.get("network_mode") == "no-network"),
        ("Trivial solutions fail (no-op, stub, partial)", all(x.get("killed") for x in fixed) if fixed else None),
        ("Wrong implementations fail (mutants killed over the bar)", (kill >= threshold) if kill is not None else None),
        ("Snapshot, canary, secret scan and diff policy are clean",
         all(_mapping(audit.get("checks")).values()) if audit.get("checks") else None),
        ("Files are tamper-evident (SHA-256 manifest verifies)",
         verify_manifest(d) if (d / "manifest.sha256").exists() else None),
    ]


def _checklist(root: Path | None) -> str:
    dirs = sorted(d for d in root.iterdir() if (d / "task.toml").exists()) if root else []
    if not dirs:
        return _empty("No packaged tasks found.", "codekiln run <repo>")
    cols = [(d.name, _checks(d)) for d in dirs]
    head = "".join(f'<th scope="col">{_e(n.removeprefix("click-"))}</th>' for n, _ in cols)
    rows = ""
    for i, (label, _) in enumerate(cols[0][1]):
        cells = ""
        for _, checks in cols:
            v = checks[i][1]
            cells += ('<td class="ok">pass</td>' if v else '<td class="bad">FAIL</td>' if v is False
                      else "<td>-</td>")
        rows += f"<tr><th scope=\"row\">{_e(label)}</th>{cells}</tr>"
    return ('<div class="scroll"><table class="tasks"><thead><tr><th scope="col">Check (task and outcome validity)'
            f"</th>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
            '<p class="path">Modelled on the Agentic Benchmark Checklist (arXiv 2507.02825). Every answer is read from a '
            "file the pipeline wrote; '-' means that file is missing, not that the check passed.</p>")


def _package(d: Path) -> dict[str, Any]:
    meta = tomllib.loads((d / "task.toml").read_text(encoding="utf-8")).get("metadata", {})
    instr = (d / "instruction.md").read_text(encoding="utf-8") if (d / "instruction.md").exists() else ""
    title = next((ln.lstrip("# ").strip() for ln in instr.splitlines() if ln.strip()), d.name)

    def count(f: str) -> int | None:
        p = d / "tests" / f
        return len(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None

    receipt_p = d / "generation_receipt.json"
    receipt = json.loads(receipt_p.read_text(encoding="utf-8")) if receipt_p.exists() else {}
    dossier_p = d / "mutation_dossier.json"
    dossier = json.loads(dossier_p.read_text(encoding="utf-8")) if dossier_p.exists() else {}
    gates = _mapping(receipt.get("gates"))
    # The packager does not emit gate rewards; read them from nested gates, and fall back to the
    # mutation dossier for the kill rate. Top-level keys win for older receipts.
    metrics = {
        "mutation_kill_rate": receipt.get("mutation_kill_rate", dossier.get("semantic_kill_rate")),
        "nop_rewards": receipt.get("nop_rewards", gates.get("nop_rewards")) or _logged(d, "nop"),
        "oracle_rewards": receipt.get("oracle_rewards", gates.get("oracle_rewards")) or _logged(d, "oracle"),
    }
    return {"name": d.name, "title": title, "instruction": instr, "meta": meta,
            "f2p": count("fail_to_pass.json"), "p2p": count("pass_to_pass.json"),
            "receipt": receipt, "metrics": metrics}


def _rewards(rs: list[float | None] | None, want: float) -> str:
    if not rs:
        return "<td>-</td>"
    ok = all(r == want for r in rs)
    shown = f"{_fmt(rs[0])} &times;{len(rs)}" if len(set(rs)) == 1 else " ".join(map(_fmt, rs))
    return f'<td class="{"ok" if ok else "bad"}">{shown}</td>'


def _packages(root: Path | None) -> str:
    dirs = sorted(d for d in root.iterdir() if (d / "task.toml").exists()) if root else []
    if not dirs:
        return _empty("No packaged tasks found.", "codekiln run <repo>")
    rows = ""
    for p in map(_package, dirs):
        meta, m = p["meta"], p["metrics"]
        kill = m["mutation_kill_rate"]
        rows += (
            f'<tr><td><details><summary>{_e(p["title"])}</summary><pre>{_e(p["instruction"])}</pre>'
            f'<p class="path"><code>{_e(p["name"])}/</code></p></details></td>'
            f'<td>{_e(meta.get("category", "-"))}</td><td>{_e(meta.get("difficulty", "-"))}</td>'
            f'<td class="n">{_e(p["f2p"] if p["f2p"] is not None else "-")}</td>'
            f'<td class="n">{_e(p["p2p"] if p["p2p"] is not None else "-")}</td>'
            f'{_rewards(m["nop_rewards"], 0.0)}{_rewards(m["oracle_rewards"], 1.0)}'
            f'<td class="n">{"-" if kill is None else f"{100 * kill:.0f}%"}</td></tr>'
        )
    return ('<div class="scroll"><table class="tasks"><thead><tr><th scope="col">Task</th>'
            '<th scope="col">Category</th><th scope="col">Level</th><th scope="col">Feature tests</th>'
            '<th scope="col">Regression tests</th><th scope="col">nop reward (want 0)</th>'
            '<th scope="col">oracle reward (want 1)</th><th scope="col">Mutants killed</th></tr>'
            f"</thead><tbody>{rows}</tbody></table></div>")


def _graveyard(cands: list[dict[str, Any]]) -> str:
    dead = [c for c in cands if c["status"] == State.REJECTED.value]
    if not dead:
        return '<p class="empty">No candidates were rejected in this run.</p>'
    items = "".join(
        f'<li><span class="who">#{c["id"]} {_e(c["category"])} {_e(c["difficulty"])}</span> '
        f'{_e(c["rejection_reason"] or "no reason recorded")}</li>' for c in dead
    )
    return f'<ul class="graveyard">{items}</ul>'


def _models(evals: list[dict[str, Any]]) -> str:
    if not evals:
        return _empty("No model has attempted these tasks yet.", "codekiln eval <package> <model>")
    by_model: dict[str, list[int]] = {}
    for e in evals:
        by_model.setdefault(e["model_id"], []).append(int(e["resolved"] or 0))
    rows = "".join(
        f'<tr><th scope="row">{_e(m)}</th><td class="n">{sum(v)} / {len(v)}</td>'
        f'<td class="n">{100 * sum(v) / len(v):.0f}%</td></tr>'
        for m, v in sorted(by_model.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
    )
    return ('<table class="models"><thead><tr><th scope="col">Model</th><th scope="col" class="n">'
            f'Solved</th><th scope="col" class="n">Solve rate</th></tr></thead><tbody>{rows}</tbody></table>')


def build_report(
    con: sqlite3.Connection, run_id: int, packages: Path | None = None, matrix: Path | None = None
) -> str:
    run = _rows(con, "SELECT r.*, p.url, p.base_commit FROM factory_runs r "
                     "JOIN repositories p ON p.id = r.repo_id WHERE r.id = ?", run_id)
    if not run:
        raise ValueError(f"run {run_id} not found in the ledger")
    cands = _rows(con, "SELECT * FROM task_candidates WHERE run_id = ? ORDER BY id", run_id)
    evals = _rows(con, "SELECT e.* FROM eval_runs e JOIN task_candidates c ON c.id = e.candidate_id "
                       "WHERE c.run_id = ?", run_id)
    m = json.loads(matrix.read_text(encoding="utf-8")) if matrix and matrix.exists() else None
    shipped = sum(c["status"] == State.PACKAGED.value for c in cands)
    repo = Path(str(run[0]["url"]).rstrip("/")).name
    commit = str(run[0]["base_commit"])
    lede = f"{shipped} of {len(cands)} candidates from {_e(repo)} made it through every gate."
    if m:
        lede += (" Each one asks for different behaviour." if m["distinct"]
                 else " Some of them overlap; see the matrix.")
    body = (
        f'<section aria-labelledby="h-funnel"><h2 id="h-funnel">Where candidates stopped</h2>'
        f"{_funnel(cands)}</section>"
        f'<section aria-labelledby="h-matrix"><h2 id="h-matrix">Do the tasks differ?</h2>'
        f"{_matrix(m)}</section>"
        f'<section aria-labelledby="h-tasks"><h2 id="h-tasks">Shipped tasks</h2>'
        f"{_packages(packages)}</section>"
        f'<section aria-labelledby="h-check"><h2 id="h-check">Is each task valid?</h2>'
        f"{_checklist(packages)}</section>"
        f'<section aria-labelledby="h-dead"><h2 id="h-dead">Rejected candidates</h2>'
        f"{_graveyard(cands)}</section>"
        f'<section aria-labelledby="h-models"><h2 id="h-models">How models did</h2>'
        f"{_models(evals)}</section>"
    )
    tpl = files("codekiln.templates").joinpath("report.html").read_text(encoding="utf-8")
    return (tpl.replace("{{title}}", f"CodeKiln run {run_id}: {_e(repo)}")
               .replace("{{repo}}", _e(repo)).replace("{{commit}}", _e(commit))
               .replace("{{run_id}}", str(run_id))
               .replace("{{lede}}", lede).replace("{{body}}", body))
