# Technical notes

Per-owner sections; the roadmap item for the full document is [H]. This file starts with the pipeline-core part ([L]: ingestion, planner, gate, parsers, grader).

## Pipeline core (Loktrishal K)

### Ingestion (`ingestion/repo_loader.py`)
- Accepts a git URL, a directory or a zip, and returns the pinned `base_commit` (HEAD, or `--commit`). A tree with no history becomes a one-commit repo so it still has a commit to pin.
- The source repo is untrusted (SPEC trust boundary 1). Nothing from it is executed. Git runs with `core.hooksPath` pointed at the null device and `protocol.ext.allow=never`; local repos are cloned with `--no-hardlinks`, never copied, so the source `.git/config` is not inherited. Zip entries are rejected on path escape (zip-slip) or symlink, and escaping symlinks are rejected after checkout.
- A failed load deletes the destination, so a half-loaded workspace never gets reused.
- Known ceiling: no decompressed-size cap on zips.

### Planner (`planner/`)
- **Reverse extract = locate only.** It parses the repo with `ast`, builds a file-level import graph (absolute, relative and `src/`-layout imports), and for each module with public symbols takes its dependents by BFS over reverse imports (depth 2). That set is a candidate slot. It never edits or strips code: tasks start from the unmodified commit and the feature must not exist upstream.
- **Forward seeds** propose where a new feature would go: one seed per target click subsystem (types, parser/options, formatting/help, shell completion, testing/CliRunner, termui), emitted only when those module files exist, then a few modules per category, plus cross-directory import edges for `CROSS_MODULE`. The synthesizer (not built yet) turns a seed into a feature spec.
- **Categories:** Core Logic, API/CLI, Validation, Persistence, Cross-Module, assigned from filename hints, or Cross-Module when dependents span directories.
- **Diversity guard:** a candidate's identity is its file set plus its public symbols. A candidate is rejected if its Jaccard overlap with any accepted one is >= 0.4. Selection round-robins across categories. The SHA-256 of the sorted items is stored as `ast_fingerprint`.
- **Difficulty dial:** L3 if the plan touches >= 5 files, >= 3 directories, or has >= 5 dependents; L2 if >= 2 files; otherwise L1. `balance` picks slots at a 1:1:1 L1:L2:L3 mix and tops up unfilled levels from the rest.
- `scan` skips tests, `examples/`, `docs/`, venvs and build output, and any file that does not parse.
- No LLM is used in this stage. It is deterministic for a given commit.
- Known ceilings: static imports only (no coverage weighting, no dynamic imports); the diversity pass is single-shot, so a plan skipped early is not retried after later accepts.

### 3-state gate (`validation/three_state_gate.py`)
- Execution-based. The gate only reads per-test statuses from an injected `Runner(phase) -> {node: status}`; nothing an LLM says counts.
- State A: the base commit's tests must all pass; the passing nodes define P2P. State B: base + F2P tests, run x3, every F2P must fail and P2P must still pass (a passing F2P means the feature already exists; a missing one means the harness crashed). State C: base + reference patch, x3, everything passes.
- **Flakiness:** a P2P node whose result differs between repeats is quarantined and excluded, not fatal. A flaky F2P node rejects the candidate. Quarantine is written to JSON.
- The runner is meant to be backed by `harbor run` (nop/oracle) from the packaging side, the same commands the judges use.

### Parsers (`validation/parsers.py`)
CTRF and JUnit XML to `{node_id: passed|failed|skipped}`. Unknown statuses count as failed, never as passed. Known ceiling: stdlib XML is not hardened, so swap in `defusedxml` if reports can come from untrusted code.

### Mergeability grader (`evaluation/mergeability_grader.py`)
Compares an agent diff to the reference diff: LOC bloat must be < 2.0x, the agent may touch no file the reference did not (blast radius 0), and caller-measured lint and type deltas must not worsen. No complexity delta yet.

### Reuse and tools
Standard library only for this part: `ast`, `subprocess` (git), `zipfile`, `hashlib`, `sqlite3`, `json`, `xml`. No third-party dependency and no model call.

### Manual-fix log
- **Nov 27, found by running on real click:** `classify` matched hints on the full path, so every file under `src/click/` was labelled API/CLI (the `cli` hint matched "click"), and `examples/` demo scripts were being planned as features. The synthetic test repo had hidden both. Fixed in #17: match on the filename only and skip `examples/` and `docs/`.

## Sandbox, packaging & DevOps (Cholan Abhaynadh Kinnera)

### Profiler (`ingestion/profiler.py`)
- **Static-only inspection (SPEC trust boundary 1):** reads `pyproject.toml` via stdlib `tomllib`, `.python-version`, `setup.py`/`setup.cfg`, `uv.lock`/`poetry.lock`, `requirements*.txt`, and `package.json` without importing or executing any target repo code.
- **Safety guards:** resolves manifest paths against `root.resolve()` to reject symlink escapes, enforces a 2 MB manifest read cap, validates Python version pins against an allowlist (`3.10`–`3.13`, default `3.12`), and restricts extras (`dev`/`test`/`tests`/`testing`) and requirements paths to safe character regexes so untrusted TOML strings cannot inject Dockerfile directives.
- **Known ceilings:** `setup.py` / `setup.cfg` are detected for `pip` installs rather than AST-evaluated for dynamic extras; Wave 2's Node/TypeScript adapter was cut once the rules pinned the target to `pallets/click`.

### Cached env bootstrap & templates (`ingestion/env_bootstrap.py`, `templates/dockerfiles/python.Dockerfile.j2`)
- **One cached image per `(repo_id, commit, arch)`:** `image_tag` hashes `repo_id|commit|docker_platform` across normalized `linux/amd64` and `linux/arm64` targets. `bootstrap_env` checks `docker image inspect` (verifying both image existence and matching architecture) before building, and upserts `stack` + `image_digest` into SQLite `repositories`.
- **Clean build context & non-root execution:** the rendered `Dockerfile` is written to a temporary directory outside the target repository so the working tree stays clean. The image runs as non-root `codekiln` (`UID 10001`), installs into `/opt/venv`, and ensures `pytest` is installed.
- **Template engine (`templates/__init__.py`):** `Jinja2` `FileSystemLoader` with `StrictUndefined`, shared by Dockerfile and Harbor templates.

### Harbor execution, packaging & reproducibility
- **Judge parity (course correction):** instead of a separate `docker-py` runner, verification and trials execute through Harbor 0.25 (`packaging/harbor.py`, `validation/harbor_runner.py`, `validation/gate_runner.py`, `packaging/scrubber.py`, `packaging/packager.py`), ensuring `nop -> 0.0` and `oracle -> 1.0` match the judges' exact evaluation path (`network_mode = "no-network"`, single orphan commit, editable install, SHA-256 manifest, atomic `os.replace()`).
- **Reproducible factory runner & CI hardening (PR #55):** root `Dockerfile` + `.dockerignore` (non-root `UID 10001`, `harbor==0.25.*`, `docker-cli`, `docker-buildx`, no baked secrets), `scripts/demo_run.sh` (end-to-end CLI driver + `--dry-run`), and `.github/workflows/ci.yml` (`permissions: contents: read`, tracked secret/env guard, shell check, `docker-smoke` build check).

### Reuse and tools
- `Jinja2` (`StrictUndefined` templates), `docker` CLI + `docker-buildx` (multi-arch builds and `inspect` cache checks), Harbor `0.25.*`, stdlib `tomllib`, `hashlib`, `subprocess`, `tempfile`, `sqlite3`. No LLM call in these modules.

### Manual-fix log
- **Nov 27, symlink test on Windows:** `test_profile_rejects_invalid_toml_missing_dir_and_escaping_symlink` failed where unprivileged Windows hosts disallow symlinks; updated in #33 to skip symlink creation when `OSError` is raised while keeping Linux CI enforcement active.
- **Nov 27, `repo_url` validation vs local paths:** `packaging/harbor.py` enforces `https://...` on `repo_url`, so `scripts/demo_run.sh` defaults to `https://github.com/pallets/click` in full mode and reserves local paths for `--dry-run`.
