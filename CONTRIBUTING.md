# Contributing

## Workflow

`main` only changes through pull requests.

```bash
git switch main && git pull
git switch -c feat/<short-name>
# work, commit small
git push -u origin HEAD
gh pr create --fill
```

One roadmap item per PR. CI must be green (ruff, mypy, pytest, size guard).

## Keep the repo small

Source code and docs only. Generated or heavy things stay local and are covered by `.gitignore`:

- supplied target repos (`workspaces/`, `repos/`), run output (`runs/`, `staging/`, `packages/`, `reports/`)
- SQLite DBs, ledgers, CTRF output, logs
- archives, videos, PDFs, decks, model weights, `node_modules/`, venvs

Rules:

- CI fails any tracked file over 1 MB. If you need a fixture, make it tiny; for anything larger, generate it in a test.
- Never `git add .` blindly; check `git status` first.
- Never commit `.env` or keys. Use `.env.example`.
- Demo videos/decks go in the hackathon portal, not git.

## Local checks

```bash
ruff check . && mypy && pytest
```
