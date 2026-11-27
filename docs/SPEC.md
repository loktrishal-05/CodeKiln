# CodeKiln Spec

Distilled from the *Innodata Coding Task Factory Master Report* (Innodata Frontier AI Hackathon 2026, Track 1; the report calls the product "CodeForge Factory", we call it **CodeKiln**). The PDF is not committed; this file is the working source of truth.

## Official rules (override everything below)

From the organizers' *Participant Guide & Technical Specifications* (hack2skill dashboard -> Resources). Where this spec and the guide disagree, the guide wins.

- **One repo, chosen from three**: `pallets/click` (pytest), `colinhacks/zod` (vitest), `open-source-parsers/jsoncpp` (ctest). **We use `pallets/click`** because our planner is Python-AST based. Base: tag `8.5.1`, commit `cf19179e8e2fd54c601378cec9e0d21bb12b6fb1` (verified 2026-11-27: 2434 passed, 24 skipped, 0 failed on Python 3.12).
- **Feature tasks only.** No bug fixes. Every task starts from the **unmodified** base commit, and the generated feature **must not exist upstream**. So we never strip existing code to make a task: reverse extraction is a planning signal only (see Discovery).
- **5 distinct tasks** from the repo, differing in behaviour, rules or interactions (rewording/renaming does not count).
- **Harbor CLI 0.25.x** (`pip install "harbor==0.25.*"`); its task schema is `schema_version = "1.4"`. Judges run, per task:
  - `harbor run -p <task> -a nop -k 1 -o <out> --no-delete -y` -> reward **0.0**
  - `harbor run -p <task> -a oracle -k 1 -o <out> --no-delete -y` -> reward **1.0**
- **Reward formula** (our `tests/test.sh` must implement exactly this): any P2P (regression) test fails -> `0.0`; else `passed_F2P / total_F2P`. A missing reward = environment crash.
- **Rubric** (hack2skill page): Working Implementation 25%, Accuracy & Validation 25%, Meaningful Variation 20%, Originality & Engineering 15%, Practical Impact & Clarity 15%. Judges re-run the pipeline in a standard Docker setup; automated reliability beats hand-fixed output.
- **Deliverables**: runnable source + setup, inspectable sample packages tagged to the base commit, validation evidence (logs, metrics, failure cases, documented manual fixes, mutation testing), technical explanation (design, assumptions, reused components, **every model and tool declared**), walkthrough video, deck (template in Resources). Deadline **Dec 6, 2026, 11:55 PM IST**.
- Out of scope: tasks that create/exploit vulnerabilities, tests needing network or real credentials.

## Goal

One reusable pipeline that takes the click repo at a pinned commit and emits 5+ realistic **feature-development** task packages for evaluating AI coding systems. Judges grade the pipeline, not hand-written tasks.

## Task package (per task, Harbor layout)

```
<task>/
  instruction.md            # only file the evaluated agent sees
  task.toml                 # schema_version="1.4"; [metadata] repo, base_commit (40 chars), license,
                            #   category, difficulty; [environment] network_mode="no-network";
                            #   [verifier] environment_mode="separate"
  environment/Dockerfile    # clones click at base_commit, installs deps, all existing tests pass
  solution/solve.sh         # `git apply` of solution/reference.diff
  solution/reference.diff
  tests/test.sh             # runs F2P + P2P with pytest --ctrf /logs/verifier/ctrf.json,
                            #   writes /logs/verifier/reward.txt per the reward formula
  tests/fail_to_pass.json   # SWE-bench style node ids
  tests/pass_to_pass.json
  tests/test_<feature>.py   # the new feature tests
  generation_receipt.json   # ours: gate results, hashes, model, cost
  manifest.sha256
```

Must prove (and the gate checks by running Harbor itself): nop -> 0.0 with every P2P passing, oracle -> 1.0, each x3.

## State machine

```
QUEUED -> PROFILED -> ENV_READY -> PLANNED -> DIVERSITY_OK -> SYNTHESIZED -> BLIND_READER_OK
 -> STATE_A_P2P_OK -> STATE_B_F2P_FAIL (x3) -> STATE_C_ALL_PASS (x3) -> MUTATION_OK (>=80%)
 -> RTM_OK -> SCRUBBED -> PACKAGED (atomic move + SHA-256 manifest)
Bounded repair: max 2 retries on SYNTH/B/C; max 1 test-strengthen on MUTATION_FAIL; else REJECTED.
```

Code: `src/codekiln/states.py`.

## Functional requirement groups

| IDs | Area | Summary |
|---|---|---|
| FR-001-004 | Ingestion & env | Git URL / dir / zip; record `base_commit`; disable git hooks; detect stack + test runner; one cached Docker base image per (repo, commit) |
| FR-005-007 | Discovery & diversity | Reverse extraction (coverage + dependency graph BFS) **locates** where features live; forward synthesis proposes **new** features there (5 categories: Core Logic, API/CLI, Validation, Persistence, Cross-Module); dedupe by AST/file overlap, Jaccard < 0.4. Never delete upstream code to create a task |
| FR-008-010 | Contract-first synthesis | `instruction.md` with explicit signatures/errors; reference diff; F2P tests + P2P manifest; spec-test interface linter |
| FR-011-016 | Execution & mutation | Runner = `harbor run` (nop for A/B, oracle for C) and CTRF read back from the trial's `verifier/` dir; State A/B/C (x3 for flakiness); 4-class probes: No-Op, Hardcoded Stub, Partial-Impl, 3-5 semantic mutants (>=80% killed), each run as a Harbor trial with a swapped `solution/` |
| FR-017-027 | Packaging, eval, ops | Harbor packager + pluggable Innodata adapter, single-commit scrubber, no-network runtime, policy filter, cost ledger, separate verifier container, static HTML dashboard |

## Architecture

- **Host control plane**: holds API keys, AST/graph analysis, diversity guard, SQLite ledger, reports. Keys never enter containers.
- **Execution plane**: ephemeral non-root Docker sandboxes, `network_mode=none`, `--cap-drop=ALL`, `no-new-privileges`, pids 256, 4 GB, 2 CPU, 120 s watchdog.

| Layer | MVP | Production |
|---|---|---|
| Orchestration | Typed async Python state machine, tiered LLM router | Temporal DAGs, prompt caching, vLLM |
| Sandbox | Harbor 0.25 docker environment (same path the judges use) | Harbor on Modal / Daytona / K8s |
| Storage | SQLite WAL (5 tables) + atomic FS packages | PostgreSQL 16, object-locked S3, OCI registry, Redis |
| Interface | Typer CLI + static HTML report | FastAPI + React HITL portal, OIDC/RBAC |

Tables: `repositories`, `factory_runs`, `task_candidates`, `validation_executions`, `eval_runs` (see `src/codekiln/db.py`).

## Operational artifacts

| Module | Artifact | Rule |
|---|---|---|
| Generation receipt | `generation_receipt.json` + `manifest.sha256` | written in `staging/`, promoted via `os.replace()` only after all gates pass |
| Cost ledger | `billing_ledger.csv` (BUDGET_ALLOCATION, PREAUTH_HOLD, LLM_DEBIT, PREAUTH_RELEASE) | hard `--max-budget-usd` circuit breaker |
| Idempotent resume | `SHA256(repo\|commit\|mode\|slot)` | `resume --run-id` skips finished slots; poison repos to dead-letter |
| Flakiness ledger | `flakiness_quarantine.json` | serial retry, quarantine confirmed flaky nodes |
| RTM audit | `rtm_map.json` | block export if any REQ has 0 F2P tests or tests import symbols not in `instruction.md` |
| Mutation dossier | `mutation_dossier.json` | 100% kill No-Op/Stub, >=80% semantic; one strengthen pass |
| Security log | `security_audit.json` | 1 commit in git, egress probe fails, no site-packages leak, diff policy, canary, secret scan |
| Eval scorecard | `eval_receipt.json` | model, resolved, bloat ratio, blast radius, ATIF trace, USD/solve |

## Variation proof (Meaningful Variation, 20%)

Jaccard on files/AST shows tasks touch different code; it does not show they ask for different behaviour. We prove that by execution:

- **Cross-task oracle matrix** (`variation_matrix.json`): for every pair (i, j), run task j's tests against task i's reference patch (a Harbor trial of task j with task i's `solution/`). Distinct tasks give 1.0 on the diagonal and ~0.0 off it; any off-diagonal score > 0.5 means i and j overlap, and the planner must replace one of them.
- **Model spread** (`eval_receipt.json`): run 2+ real agents through Harbor (`-a claude-code`, `-a codex`, ...). Tasks that every model solves or none solves are weak discriminators; report the per-task solve matrix.

## Trust boundaries

1. **Untrusted ingestion**: `core.hooksPath=/dev/null`, block zip-slip and escaping symlinks, wrap repo text in `<untrusted_repo_content>`.
2. **Container hardening**: see execution plane above.
3. **Host-only secrets**: keys never in Docker ENV or volumes; log redaction.
4. **Anti-gaming**: re-init `.git` as a single orphan commit; strip editable site-packages copies; grade only `agent.diff` in a fresh verifier container; reject diffs touching `conftest.py` / test runners.
5. **Policy guardrail**: AST diff scanner rejects CI/workflow edits, new network/subprocess calls, offensive-exploit tasks.

## Open organizer items (design for pluggability)

- Supplied repo format (Git URL vs zip) and final `task.toml` template: isolate behind adapters.
- Security-task policy: block offensive tasks by default.
