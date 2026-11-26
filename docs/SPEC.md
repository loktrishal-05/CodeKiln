# CodeKiln Spec

Distilled from the *Innodata Coding Task Factory Master Report* (Innodata Frontier AI Hackathon 2026, Track 1; the report calls the product "CodeForge Factory", we call it **CodeKiln**). The PDF is not committed; this file is the working source of truth.

## Goal

One reusable pipeline that takes a supplied working repo (target: 3 repos) and emits realistic **feature-development** task packages for evaluating AI coding systems. Judges grade the pipeline, not hand-written tasks.

Rubric weights: Validity 25%, Repeatability 25%, Variation 20%, Originality 15%, Impact/Demo 15%.

## Task package (per task)

`task.toml` (Harbor v1.4) · `instruction.md` · `solution/reference.diff` · `tests/` (F2P tests + P2P manifest) · `generation_receipt.json` · `manifest.sha256`

Must prove: (1) base tests pass, (2) feature tests fail on base, (3) reference patch passes feature + regression tests.

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
| FR-005-007 | Discovery & diversity | Reverse extraction (coverage + dependency graph BFS) and forward synthesis (5 categories: Core Logic, API/CLI, Validation, Persistence, Cross-Module); dedupe by AST/file overlap, Jaccard < 0.4 |
| FR-008-010 | Contract-first synthesis | `instruction.md` with explicit signatures/errors; reference diff; F2P tests + P2P manifest; spec-test interface linter |
| FR-011-016 | Execution & mutation | CTRF/JUnit parsing; State A/B/C (x3 for flakiness); 4-class probes: No-Op, Hardcoded Stub, Partial-Impl, 3-5 semantic mutants (>=80% killed) |
| FR-017-027 | Packaging, eval, ops | Harbor packager + pluggable Innodata adapter, single-commit scrubber, no-network runtime, policy filter, cost ledger, separate verifier container, static HTML dashboard |

## Architecture

- **Host control plane**: holds API keys, AST/graph analysis, diversity guard, SQLite ledger, reports. Keys never enter containers.
- **Execution plane**: ephemeral non-root Docker sandboxes, `network_mode=none`, `--cap-drop=ALL`, `no-new-privileges`, pids 256, 4 GB, 2 CPU, 120 s watchdog.

| Layer | MVP | Production |
|---|---|---|
| Orchestration | Typed async Python state machine, tiered LLM router | Temporal DAGs, prompt caching, vLLM |
| Sandbox | docker-py, 2-4 containers | Modal / K8s gVisor / Firecracker |
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

## Trust boundaries

1. **Untrusted ingestion**: `core.hooksPath=/dev/null`, block zip-slip and escaping symlinks, wrap repo text in `<untrusted_repo_content>`.
2. **Container hardening**: see execution plane above.
3. **Host-only secrets**: keys never in Docker ENV or volumes; log redaction.
4. **Anti-gaming**: re-init `.git` as a single orphan commit; strip editable site-packages copies; grade only `agent.diff` in a fresh verifier container; reject diffs touching `conftest.py` / test runners.
5. **Policy guardrail**: AST diff scanner rejects CI/workflow edits, new network/subprocess calls, offensive-exploit tasks.

## Open organizer items (design for pluggability)

- Supplied repo format (Git URL vs zip) and final `task.toml` template: isolate behind adapters.
- Security-task policy: block offensive tasks by default.
