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

## Docs

- [SPEC](docs/SPEC.md): architecture, state machine, artifacts, trust boundaries
- [ROADMAP](docs/ROADMAP.md): waves and PR-sized tasks
- [CONTRIBUTING](CONTRIBUTING.md): PR workflow and repo-size rules
