# CodeKiln

Python 3.12 factory that turns a repo into validated feature-task packages. Read `docs/SPEC.md` first, then `docs/ROADMAP.md` for what to build next.

- Work on a branch, open a PR; never commit to `main`.
- Gates are execution-based. Never trust LLM self-reported success.
- Secrets are host-only; never pass them into containers.
- Keep it small: no new dependency or abstraction until a roadmap item needs it. Never commit generated output (see `.gitignore`).
- Checks: `ruff check . && mypy && pytest`.
