# Roadmap

Prototype deadline **Dec 6, 2026**; finale **Dec 20, 2026** (New Delhi). Each checkbox = one PR.

## Wave 1: core MVP (Nov 27 - Dec 5)

- [x] Repo scaffold, state machine, SQLite schema, CLI skeleton
- [ ] Ingestion: repo loader (URL/dir/zip), commit pin, hooks disabled, zip-slip guard
- [ ] Profiler + Python Dockerfile bootstrap, one cached image per (repo, commit), ARM64/x86
- [ ] Sandbox runner (hardened docker-py) + CTRF/JUnit parsers
- [ ] Planner: reverse extract + forward synth + AST diversity guard
- [ ] Synthesizer: instruction.md, reference diff, F2P tests, RTM coverage gate
- [ ] 3-state x3 gate + flakiness quarantine
- [ ] 4-class mutation probes (>=80% kill)
- [ ] Scrubber + Harbor packager + generation receipt + manifest
- [ ] Cost ledger + budget breaker + idempotent resume
- [ ] Verify end-to-end on 2 supplied repos; demo script; submit repo, brief, video, deck

## Wave 2: finale differentiators (Dec 7 - Dec 19)

- [ ] Node/TypeScript adapter
- [ ] Mergeability & blast-radius grader
- [ ] L1/L2/L3 difficulty dial
- [ ] Dry-run blind reader linter
- [ ] Static HTML replay viewer + rejected-candidates graveyard
- [ ] Cross-repo portability matrix
- [ ] Blind eval leaderboard (2+ frontier models)

## Wave 3: production (post-hackathon)

FastAPI + React HITL portal, Temporal + Modal/Firecracker fleet, adaptive validation agent, memorization monitor, live PR-to-benchmark loop.
