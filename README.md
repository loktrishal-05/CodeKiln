# CodeKiln

A factory that turns a working software repo into validated, tamper-resistant **feature-development tasks** for evaluating AI coding systems. Built for the Innodata Frontier AI Hackathon 2026 (Track 1).

Every candidate task must pass execution gates before it is packaged: base tests pass, feature tests fail on the base, the reference patch passes everything (each x3 for flakiness), adversarial mutants are killed, and every requirement maps to a test. Nothing an LLM self-reports is trusted.

> Status: scaffold. CLI commands are stubs; see [docs/ROADMAP.md](docs/ROADMAP.md).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
pytest
codekiln --help
```

Requires Python 3.12+ and Docker (for the sandbox, once implemented).

## Layout

```
src/codekiln/
  cli.py config.py db.py states.py
  ingestion/   planner/   synthesis/   validation/   packaging/   evaluation/   templates/
tests/{unit,integration,security}
docs/SPEC.md  docs/ROADMAP.md
```

## Team & roles

Four contributors, five PR-sized roadmap items each, split by strength. Item details and Dec dates are in [docs/ROADMAP.md](docs/ROADMAP.md).

| Member | GitHub | Role | Owns (code paths) | Tasks |
|---|---|---|---|---|
| Lohith K | @loktrishal-05 | Backend lead, pipeline core | `ingestion/`, `planner/`, `validation/` (gate, parsers), `evaluation/` grader | Repo loader + commit pin + zip-slip guard; CTRF/JUnit parsers; planner (reverse + forward + AST diversity); 3-state x3 gate + flakiness quarantine; mergeability grader + L1/L2/L3 dial |
| Sanjjith B | @Sanjjith27 | Backend, synthesis and ledgers | `synthesis/`, `validation/mutation_probe.py`, `db.py` ledgers, eval runner | Synthesizer + RTM gate; 4-class mutation probes; cost ledger + budget breaker + idempotent resume; blind-reader linter; blind eval runner + `eval_receipt.json` |
| Harsha | @harsha-code-per | Frontend, reports and demo | `evaluation/report_builder.py`, `templates/` (HTML), submission assets | Static HTML dashboard; replay viewer + graveyard; cross-repo portability matrix; leaderboard view; brief, video, deck |
| Cholan | [@Cholan-kinnera](https://github.com/Cholan-kinnera) | Cloud / DevOps, sandbox and packaging | `ingestion/` profiler + Docker bootstrap, sandbox runner, `packaging/`, `templates/dockerfiles`, CI, `scripts/` | Profiler + cached images (ARM64/x86); hardened docker-py sandbox; scrubber + Harbor packager + receipt + `security_audit.json`; demo script + CI; Node/TS adapter |

Hand-offs: Cholan's sandbox runner feeds Lohith's gate; Lohith's gate and Sanjjith's mutation probes both write `validation_executions`; Sanjjith's JSON outputs (`rtm_map.json`, `mutation_dossier.json`, `eval_receipt.json`) are the contract Harsha's report reads, so agree on those schemas first. Harsha stays dependency-free (plain HTML/CSS/JS, no build step) until Wave 3.

Ground rules: one branch and PR per roadmap item, tick the checkbox in the PR, another member reviews. Dec 4-5 is an all-hands end-to-end verification on the 2 supplied repos.

## Docs

- [SPEC](docs/SPEC.md): architecture, state machine, artifacts, trust boundaries
- [ROADMAP](docs/ROADMAP.md): waves and PR-sized tasks
- [CONTRIBUTING](CONTRIBUTING.md): PR workflow and repo-size rules
