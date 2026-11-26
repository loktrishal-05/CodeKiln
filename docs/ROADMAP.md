# Roadmap

Prototype deadline **Dec 6, 2026**; finale **Dec 20, 2026** (New Delhi). Each checkbox = one PR. Owner tags: **[L]** Lohith, **[S]** Sanjjith B, **[H]** Harsha, **[C]** Cholan. See the README for the team table and hand-offs.

## Wave 1: core MVP (Nov 27 - Dec 5)

- [x] Repo scaffold, state machine, SQLite schema, CLI skeleton
- [ ] **[L]** Ingestion: repo loader (URL/dir/zip), commit pin, hooks disabled, zip-slip guard
- [ ] **[C]** Profiler + Python Dockerfile bootstrap, one cached image per (repo, commit), ARM64/x86
- [ ] **[C]** Sandbox runner (hardened docker-py); **[L]** CTRF/JUnit parsers
- [ ] **[L]** Planner: reverse extract + forward synth + AST diversity guard
- [ ] **[S]** Synthesizer: instruction.md, reference diff, F2P tests, RTM coverage gate
- [ ] **[L]** 3-state x3 gate + flakiness quarantine
- [ ] **[S]** 4-class mutation probes (>=80% kill)
- [ ] **[C]** Scrubber + Harbor packager + generation receipt + manifest + `security_audit.json`
- [ ] **[S]** Cost ledger + budget breaker + idempotent resume
- [ ] **[H]** Static HTML dashboard (`codekiln report` -> `factory_report.html`)
- [ ] **[C]** `scripts/demo_run.sh` + CI hardening; verify end-to-end on 2 supplied repos (all, Dec 4-5)
- [ ] **[H]** Submission assets: brief, video, Hack2Skill deck (all review)

## Wave 2: finale differentiators (Dec 7 - Dec 19)

- [ ] **[C]** Node/TypeScript adapter (Dockerfile template, runner, parser)
- [ ] **[L]** Mergeability & blast-radius grader
- [ ] **[L]** L1/L2/L3 difficulty dial
- [ ] **[S]** Dry-run blind reader linter
- [ ] **[H]** Static HTML replay viewer + rejected-candidates graveyard
- [ ] **[H]** Cross-repo portability matrix (UI; data from `[S]` eval/report JSON)
- [ ] **[S]** Blind eval runner + `eval_receipt.json`; **[H]** leaderboard view (2+ frontier models)

## Wave 3: production (post-hackathon)

- **[H]** React HITL portal + **[L]** FastAPI backend (OIDC/RBAC)
- **[C]** Temporal + Modal/Firecracker sandbox fleet, PostgreSQL/S3/registry
- **[S]** Adaptive validation agent, memorization monitor
- **[L]** Live PR-to-benchmark loop
