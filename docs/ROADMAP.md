# Roadmap

Prototype deadline **Dec 6, 2026, 11:55 PM IST**; finale **Dec 20, 2026** (New Delhi). Each checkbox = one PR. Owner tags: **[L]** Loktrishal K, **[S]** Sanjjith B, **[H]** Harshavardhan K, **[C]** Cholan Abhaynadh Kinnera. See the README for the team table and hand-offs.

## Course correction (Nov 27, from the official Participant Guide)

The organizers' guide (summarised in `docs/SPEC.md` -> Official rules) changes four things:

1. **Target = `pallets/click` @ 8.5.1** (`cf19179e8e2f`). We pick exactly one of click/zod/jsoncpp; the Node/TS adapter is cut.
2. **No reverse-stripped tasks.** Tasks start from the unmodified commit and the feature must not exist upstream. Reverse extraction only locates where a new feature should go.
3. **Harbor is the sandbox.** Judges run `harbor run -a nop` (expect 0.0) and `-a oracle` (expect 1.0). Our gate runs the same commands, so a package that passes our gate passes theirs. No separate docker-py sandbox.
4. **Variation must be proven by execution**: cross-task oracle matrix + real-model solve spread (SPEC -> Variation proof).

## Wave 1: core MVP (Nov 27 - Dec 5)

- [x] Repo scaffold, state machine, SQLite schema, CLI skeleton
- [x] **[L]** Ingestion: repo loader (URL/dir/zip), commit pin, hooks disabled, zip-slip guard
- [x] **[C]** Profiler + cached env image per (repo, commit), ARM64/x86
- [x] **[C]** Harbor `environment/Dockerfile` template for click @ base_commit (all existing tests pass in-image) (`packaging/harbor.py`, by [H])
- [x] **[C]** Harbor runner: `harbor run -a nop|oracle` wrapper -> reward + per-test CTRF from `verifier/` -> feeds the gate's `Runner` (`codekiln verify`); [x] **[L]** CTRF/JUnit parsers
- [x] **[L]** Planner: reverse extract + forward synth + AST diversity guard
- [x] **[L]** Planner: reverse mode becomes locate-only (no stripping); seeds target click subsystems (types, parser/options, formatting/help, shell completion, testing/CliRunner, termui)
- [x] **[S]** Synthesizer: instruction.md, reference diff, F2P tests, RTM coverage gate (emits the Harbor layout in SPEC -> Task package)
- [x] **[L]** 3-state x3 gate + flakiness quarantine
- [x] **[S]** 4-class mutation probes (>=80% kill), each probe = a Harbor trial with a swapped `solution/`
- [x] **[C]** Scrubber + Harbor packager (`tests/test.sh` with the official reward formula, `fail_to_pass.json`/`pass_to_pass.json`, `network_mode="no-network"`, separate verifier) + generation receipt + manifest + `security_audit.json`
- [x] **[S]** Cost ledger + budget breaker + idempotent resume
- [ ] **[H]** Static HTML dashboard (`codekiln report` -> `factory_report.html`)
- [ ] **[C]** One-command reproducible run (`scripts/demo_run.sh`, factory Dockerfile so judges can re-run us) + CI hardening; verify end-to-end on click (all, Dec 4-5)
- [ ] **[H]** Cross-task oracle matrix (`codekiln matrix` -> `variation_matrix.json`) + heatmap in the dashboard
- [ ] **[H]** `samples/`: the 5 final click packages + their Harbor nop/oracle logs, committed (exception to the no-generated-output rule)
- [ ] **[H]** `docs/TECHNICAL.md`: design decisions, assumptions, reused components, every model/tool used, variation strategy, manual-fix log
- [ ] **[H]** Submission assets: video, Hack2Skill deck from the Resources template (all review)

## Wave 2: finale differentiators (Dec 7 - Dec 19)

- ~~**[C]** Node/TypeScript adapter~~ cut: rules allow one repo
- [x] **[L]** Mergeability & blast-radius grader
- [x] **[L]** L1/L2/L3 difficulty dial
- [x] **[S]** Dry-run blind reader linter
- [ ] **[H]** Static HTML replay viewer + rejected-candidates graveyard
- ~~**[H]** Cross-repo portability matrix~~ replaced by the cross-task oracle matrix (Wave 1)
- [x] **[S]** Blind eval runner + `eval_receipt.json`; **[H]** leaderboard view (2+ frontier models)

## Wave 2b: agentic upgrade (Dec 7 - Dec 19)

Rule: agents propose, code decides. Every pass/fail stays in the gates, probes, scrubber and Harbor reward; each agent sits behind a flag and must beat the single-writer baseline on the same slots to stay. Gate: do not start until 5 real packages pass Harbor nop 0.0 / oracle 1.0 (issues #34-#39). Research and rationale: the "CodeKiln Upgrade Research" doc.

- [x] **[L]** Orchestrator agent over the state machine (allowed actions only, step limit, budget) (#40)
- [x] **[L]** Repo Scout: context packs per click subsystem (#41)
- [x] **[L]** Contamination firewall: later click releases/issues/PRs + memorisation probe (#42)
- [x] **[L]** Difficulty calibration from eval results, feeding the L1/L2/L3 dial (#43)
- [ ] **[S]** Feature Ideator + Critic (#44)
- [ ] **[S]** Test Author + Test Critic: alternative correct solution, flag too-narrow tests (#45)
- [ ] **[S]** Adversarial model-written mutants in the mutation probes (#46)
- [ ] **[S]** Blind solver panel + ambiguity report (#47)
- [x] **[C]** Red-team run in Harbor: an agent tries to cheat each package (#48)
- [x] **[C]** Separate verifier container (#49)
- [ ] **[H]** Live agent trace view in the dashboard (#50)
- [ ] **[H]** Agentic Benchmark Checklist report per package (#51)

## Wave 3: production (post-hackathon)

- **[H]** React HITL portal + **[L]** FastAPI backend (OIDC/RBAC)
- **[C]** Temporal + Modal/Firecracker sandbox fleet, PostgreSQL/S3/registry
- **[S]** Adaptive validation agent, memorization monitor
- **[L]** Live PR-to-benchmark loop (#54)
- **[L]** Bring-your-own-repo mode: private, contamination-free evals for a team's own codebase (#52)
- **[C]** zod (vitest) / jsoncpp (ctest) adapters + cloud sandbox fleet (#53)
