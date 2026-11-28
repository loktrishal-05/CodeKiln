# Technical notes

CodeKiln turns one open-source repo at a pinned commit into validated feature-task packages for evaluating AI coding agents. The target is `pallets/click` 8.5.1 (`cf19179e8e2f`). This document covers the design decisions, the assumptions, every component and model we use, how variation is produced and proved, and every manual fix we had to make. Facts here come from real runs; where something was not measured, it says so.

## Overview

```
repo @ pinned commit
  -> ingest (untrusted: nothing executed)          deterministic
  -> plan (AST import graph, 6 slots, L1/L2/L3)    deterministic
  -> write (LLM: feature, tests, EDITS)            model proposes
  -> build diff from the real files, prove it applies   code
  -> blind-reader lint, RTM audit, validate        code
  -> Harbor State A x1, B x3 (nop), C x3 (oracle)  execution
  -> mutation probes (5 classes, >= 80% killed)    execution
  -> scrub, manifest, receipt, atomic promote      code
  -> PACKAGED  (or REJECTED with the exact reason; a rejected candidate gets <= 2 evidence-based repairs)
```

**The one rule:** a model may propose; only code and execution decide. The ledger status of each candidate (SQLite) picks its next stage, so a run can be stopped and resumed, and no status is ever set from what a model says.

**Result of the real run (run 10, click 8.5.1, Groq `openai/gpt-oss-120b`, 8 slots):** 5 packaged, 3 rejected. All five are in `samples/`, with their nop and oracle results in `samples/harbor_logs/`. `samples/factory_report.html` is the dashboard (`codekiln report`) and `samples/variation_matrix.json` the cross-task proof.

**Reproduce:** `scripts/demo_run.sh` (root `Dockerfile`, Harbor 0.25.x) or, by hand, `codekiln factory https://github.com/pallets/click --commit cf19179e8e2fd54c601378cec9e0d21bb12b6fb1 --slots 8` with `CODEKILN_LLM_*` set in a git-ignored `.env`.

## Pipeline core (Loktrishal K)

### Ingestion (`ingestion/repo_loader.py`)
- Accepts a git URL, a directory or a zip, and returns the pinned `base_commit` (HEAD, or `--commit`). A tree with no history becomes a one-commit repo so it still has a commit to pin.
- The source repo is untrusted (SPEC trust boundary 1). Nothing from it is executed. Git runs with `core.hooksPath` pointed at the null device and `protocol.ext.allow=never`; local repos are cloned with `--no-hardlinks`, never copied, so the source `.git/config` is not inherited. Zip entries are rejected on path escape (zip-slip) or symlink, and escaping symlinks are rejected after checkout.
- A failed load deletes the destination, so a half-loaded workspace never gets reused.
- Known ceiling: no decompressed-size cap on zips.

### Planner (`planner/`)
- **Reverse extract = locate only.** It parses the repo with `ast`, builds a file-level import graph (absolute, relative and `src/`-layout imports), and for each module with public symbols takes its dependents by BFS over reverse imports (depth 2). That set is a candidate slot. It never edits or strips code: tasks start from the unmodified commit and the feature must not exist upstream.
- **Forward seeds** propose where a new feature would go: one seed per target click subsystem (types, parser/options, formatting/help, shell completion, testing/CliRunner, termui), emitted only when those module files exist, then a few modules per category, plus cross-directory import edges for `CROSS_MODULE`. The synthesizer (see Synthesis below) turns a seed into a feature spec.
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

## Synthesis, factory and the real run (Loktrishal K)

### Writer (`synthesis/llm_router.py`, `synthesis/spec_generator.py`)
- **One call returns one spec** as JSON: task name, `instruction.md` with one `- REQ-n:` line per requirement, the requirement-to-test map, the feature test files, the F2P test ids and a list of **edits**. The router speaks the OpenAI-compatible `/chat/completions` protocol with the standard library only (no SDK). The API key is read from settings, is `repr`-hidden, and is never passed into a container.
- **Provider hardening, found on the real run:** an explicit `User-Agent` (Python's default is blocked with HTTP 403 / Cloudflare 1010 by Groq); retry on 429/502/503/504 up to 4 attempts, waiting the provider's `Retry-After` or its "try again in 1m3s" text (capped at 70 s); the provider's own error text is surfaced with anything key-shaped redacted.
- **Prompt:** repo-derived text (Repo Scout pack, seeds) sits inside an `<untrusted_repo_content>` wrapper. It carries a worked example that a unit test holds to the same gates as real output (`validate` + RTM + blind reader), so the example cannot silently teach something invalid, and seven format rules, each added after a real failure (see the manual-fix log): bare test file names, ids without `tests/feature/`, edits instead of a diff, imports inside the new function (click tests that importing it stays light), at least 3 real decisions and 3 tests, and tests may only call names the patch adds.
- **Format repair (inside the writer):** up to 2 retries, each sending the original prompt plus only the *latest* rejected reply and the reason. Appending every attempt grew the request until Groq's free tier refused it with HTTP 413.

### Edit-based diffs (`synthesis/edits.py`)
Models cannot be trusted to write a correct unified diff: on the real run most failed with wrong hunk counts or wrong context. So the model says *where* code goes by symbol and code builds the patch:
`add_import`, `insert_after <existing top-level def/class>`, `append`, `new_file` (at most 12 edits, additive only, never `tests/`, `conftest.py` or CI files). Code reads the real file, locates the symbol with `ast`, builds the diff with `difflib`, and proves it with `git apply --check` before anything is written. An unknown symbol returns the list of real symbols, which is what a repair needs. CRLF files are refused (a patch against CRLF text would not apply on Linux).

### Static gates before any package is written
`validate` (kebab-case task name, F2P ids belong to the feature tests, no deletions, no test-infra edits, well-formed patch), the **RTM audit** (every requirement maps to at least one F2P test that exists, found by `ast`; imports in the tests must be named in the instruction) and the **blind reader** (lints `instruction.md`, the only file the evaluated agent sees). A spec failing any of them never becomes a package.

### Repo Scout (`synthesis/scout.py`)
A bounded, deterministic context pack per slot: public signatures with first docstring line, how many files depend on each, and the names of existing tests, so a new feature and its tests look like they belong. Static (nothing imported or run) and size-capped.

### Execution-guided repair (`repair.py`)
When a gate rejects a candidate at State C or in the mutation probes, the writer gets a corrected attempt **shown the evidence the gate produced**: pytest's `FAILURES` section from the failing State C trial (or, when pytest ran no test because the patch broke `import click`, the end of its log), or the names of the wrong implementations that still passed, plus guidance when too few could be built. Evidence is capped at 2500 characters to stay under a provider's per-request token limit.
- At most **2** repairs per candidate (`task_plans.repairs`), billed to that candidate and budget-guarded. A repaired package re-enters the pipeline at `BLIND_READER_OK`, so it passes every gate again from the start. Firewall and packaging rejections are never repaired.
- The model is never asked whether its work is good; it is only shown what happened.
- **Measured (run 10):** 5 of 8 candidates packaged: 3 passed with no repair and 2 needed both repairs. The 3 rejected ones had also used both repairs. Run 9 (one repair, less evidence) packaged none.

### Mutation probes (`validation/mutation_probe.py`)
Five classes run as real Harbor oracle trials with a swapped `solution/`: No-Op, Hardcoded Stub and Partial-Impl must be killed 100%; **semantic** mutants (flip `True/False`, `==`, `<`, `>`, `>=`, `<=`, `and`/`or`, `is not`, `0/1`, `isinstance`, `+/-`, or rename a def/parameter when a patch has few literals) must be killed at least 80% and at least 3 must be buildable; **adversarial** model-written mutants join them (Sanjjith's part). A mutant is killed when its reward is below 1.0.
Fix found on the real run: mutants edited the first match, which for a documented patch is its docstring; such a "mutant" equals the original and can never be killed, and good tests scored 0%. Mutants now touch only code (comments, docstrings and string contents are masked), and equivalent mutants (`not not x`) are skipped.

### Factory driver and orchestrator (`factory.py`, `orchestrator.py`)
`codekiln factory <repo>` plans, then loops: the ledger status of each candidate names its next stage (`verify`, `probe`, `package`); a stage that does not move its candidate is not retried in that call; a hard step bound applies. Re-running resumes from the ledger and is a no-op when finished. `--agent` lets a model *order* the allowed actions, but code enforces the offered list, falls back to the fixed order on any bad answer or provider failure, and the stage that runs is always the one the status dictates. Harbor runners are injected so the whole loop is tested without Docker.

### Contamination firewall and calibration (`validation/firewall.py`, `evaluation/calibration.py`, `evaluation/irt.py`)
- **Firewall:** rejects a feature already present in a later release of the repo (structural match on `def`/`class`/parameter names, not bare words), offline, from caller-supplied checkouts. Implemented and unit-tested; **not exercised in the real run** because no later click checkout was supplied (`--corpus`).
- **Calibration:** compares the planner's L1/L2/L3 guess with measured solve rates; a Rasch (1PL) model with ridge-regularised Newton steps handles models that did not run every task, and flags inverted tasks. Implemented and unit-tested; **not run on real data**, because we have not evaluated the five packages with several real agent models yet.

### Real runs on click 8.5.1 (Groq `openai/gpt-oss-120b`, free tier, Harbor 0.25, Docker)
| Run | Slots | Packaged | What it showed |
|---|---|---|---|
| 8 | 6 | 0 | First full run. The gates rejected all six for real reasons: a new top-level `import json` broke click's own `test_light_imports`; tests that called arguments the patch never added; tests too weak to kill 80% of mutants. |
| 9 | 6 | 0 | First run with repair. It re-tried every candidate once and still rejected all six. Cause: the repair showed pytest's `FAILURES` section only, so a patch that broke `import click` (pytest ran no test) told the model nothing. |
| 10 | 8 | **5** | Repair shows the log tail on import errors, allows 2 repairs, prompt asks for >= 3 decisions, mutation probe no longer edits docstrings. 5 packaged (repairs used: 0, 2, 2, 0, 0), 3 rejected (kill rate 67%, kill rate 60%, a feature test that fails with its own reference patch). |

Docker time was roughly 6 hours across the three runs (estimated, not instrumented), mostly spent finding and fixing the bugs below. LLM spend on the free tier is recorded as $0 in the append-only billing ledger.

### Reuse and tools
Python standard library (`urllib`, `difflib`, `ast`, `sqlite3`, `subprocess`), Typer (CLI), Harbor 0.25.x, Docker, `git apply`. No LLM SDK, no third-party diff library.

### Manual-fix log (found by running for real, all with a regression test)
- **Nov 27, #71:** every Groq call returned 403: Python's default `User-Agent` is blocked by Cloudflare. Explicit UA.
- **#72:** HTTP 429 on the free tier ended the run. Retry with the provider's own wait time.
- **#73:** repairs that appended every previous reply grew past Groq's 8000-token request cap (HTTP 413). Only the latest failed reply is sent.
- **#74:** the model repeatedly broke the format (test ids with `tests/feature/`, ids missing from the instruction). Worked example + format rules on every repair + a check that the example itself passes our gates.
- **#75:** a valid spec crashed on a non-breaking hyphen on Windows: `Path.write_text` defaults to cp1252. Every read and write in `src/` now names `encoding="utf-8"`; a test scans the source to keep it that way.
- **#76:** Windows turned `\n` into `\r\n` in written package files, which corrupted patches and made shell scripts unrunnable. `newline="\n"` everywhere, and a test builds a package and asserts it has no CRLF.
- **#78:** models wrote invalid unified diffs, and Git for Windows (`core.autocrlf=true`) checked click out with CRLF. The writer now sends edits and code builds the diff; the loader clones with `core.autocrlf=false`.
- **Nov 28, #80:** (1) repair hid import errors (above); (2) the mutation probe mutated docstrings, so good tests scored 0%; (3) too few mutants for patches built from decisions rather than literals.
- **Found while reviewing teammates' work, fixed on their PRs:** the red-team `read-hidden` attack applied `/solution/reference.diff`, which is mounted only for Harbor's oracle agent (verified with a real trial), so it would have blocked every good package (#77); the isolated verifier decoded Docker output with the platform codepage (#79).
- **Process incident, disclosed:** one early commit (#40) landed on `main` by mistake after a failed shell command skipped the branch checkout; CI was green and it was disclosed immediately. Every commit since is preceded by a check that the current branch is a feature branch.

## Agents and extra validation (Sanjjith B)
Rule for all of these: the model proposes, code gates, Harbor decides.
- **Feature Ideator + Critic (`synthesis/ideator.py`, #44):** N proposals per slot; a deterministic critic applies the same static gates (validate, RTM, blind reader), then keeps only proposals that are realistic for the slot and novel against the single-writer baseline and against earlier kept ones. It never decides execution correctness.
- **Test Author + Test Critic (`validation/test_critic.py`, #45):** the critic copies the package, swaps in an *alternative* correct implementation proposed by a model and runs Harbor. Reward 1.0 is evidence the tests accept more than one implementation; below 1.0 is only a signal for human review (the alternative may itself be wrong), so nothing is gated on it automatically.
- **Adversarial mutants (`validation/adversarial.py`, #46):** plausible-but-wrong model-written diffs join the mutation probes after code checks they are valid, distinct and near the reference. A survivor is recorded as a test gap.
- **Blind solver panel + ambiguity report (`evaluation/blind_panel.py`, #47):** two or three agents solve from `instruction.md` alone; code compares the contractual choices in their diffs (added signatures, defaults, touched files, required literals) and reports disagreements as questions for the spec. Syntactic and deterministic.
- **Real-model synthesis under the budget cap (#36):** the writer runs through a metered POST that debits every call to the billing ledger and stops on the cap.
- Not measured: how much the ideator, critic or panel improves yield over the single writer. The real run used the single writer plus repair.

## Variation strategy
Variation is produced by construction and proved by execution:
1. **By construction:** the planner spreads slots over categories (Core Logic, API/CLI, Validation, Persistence, Cross-Module) and difficulty levels (1:1:1 L1/L2/L3), and a diversity guard rejects any candidate with Jaccard overlap >= 0.4 against an accepted one (file set plus public symbols). Forward seeds name different click subsystems.
2. **By execution:** `codekiln matrix` runs each package's tests against every other package's reference solution (Harbor oracle). Distinct tasks score 1.0 on the diagonal and about 0.0 off it. **Measured on the five packages: a perfect diagonal, 1.00 on it and 0.00 on all 20 off-diagonal cells, no overlaps, nothing broken** (`samples/variation_matrix.json`, heatmap in `samples/factory_report.html`). Honest limit: the five packages are three `core_logic`, one `api_cli` and one `api_testing`, **all level L2**. The planner aims for a 1:1:1 L1/L2/L3 mix, but the packages that survived the gates were all L2 (the rejected candidates were L1 and L3), so the difficulty spread we planned is not what we shipped.
3. **By measurement (not yet done):** solve-rate spread across several real models, and Rasch calibration of the difficulty dial. The code exists; the evaluation has not been run.

## Models and tools used
| Role | Tool | Notes |
|---|---|---|
| Feature writer, repair | Groq `openai/gpt-oss-120b` | OpenAI-compatible API; free tier, rate-limited; key host-only |
| Ideator, critics, adversarial mutants, blind panel, red-team proposer, orchestrator | any OpenAI-compatible model via `CODEKILN_LLM_*` | proposals only; none was needed for the five packages except the writer |
| Sandbox and grading | Harbor 0.25.x on Docker | `nop` must score 0.0, `oracle` 1.0, the judges' exact commands |
| Hardened re-grade | `codekiln verify-isolated` | fresh container, `--network none`, `--cap-drop ALL`, `no-new-privileges`, pids/memory/cpu caps |
| Red team | `codekiln red-team` | four deterministic attacks (edit tests, read hidden, hardcode, tamper verifier). **Measured: all 20 attacks (4 x 5 packages) scored 0.0; no package blocked** (`samples/red_team/`) |
| Explainer video narration | Windows offline voice + ffmpeg | no model, no credits |

## Assumptions and known limits
- Python repos with pytest; one repo (click). The Node/TypeScript adapter was cut once the rules pinned the target.
- The writer is a single free-tier model. Yield on click was 5 of 8 candidates after repair; we expect other repos and models to differ.
- Mutation operators are textual and Python-specific; a patch with few decisions gets few mutants and is rejected rather than waved through.
- Docker time is estimated, not instrumented. Costs are $0 because the free tier is priced at zero in the ledger.
- The firewall, calibration and the multi-model solve-rate evaluation are implemented but not exercised on real data.

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
