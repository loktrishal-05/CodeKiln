# Samples: the five validated click packages

Real output of `codekiln factory` on click 8.5.1 (pinned commit `cf19179e8e2f`), written by Groq `openai/gpt-oss-120b`
and accepted only by execution: Harbor States A/B/C (per-test), the mutation probes, the scrubber, a sealed manifest.

| Package | Repairs used | Mutants killed |
|---|---|---|
| click-completion-enabled | 0 | 100% |
| click-parse-bool | 2 | 100% |
| click-parse-json-param | 2 | 100% |
| click-prompt-int-range | 0 | 80% (4 of 5; exactly the bar) |
| click-result-contains | 0 | 100% |

Each folder is a Harbor task (`instruction.md`, `environment/`, `tests/`, `solution/`, `task.toml`) (sealed and unmodified) plus the evidence:
`mutation_dossier.json`, `generation_receipt.json`, `manifest.sha256`, and `samples/harbor_logs/<package>/nop.json` (no change: reward 0.0, the
feature tests fail) and `samples/harbor_logs/<package>/oracle.json` (reference patch: reward 1.0, every test passes).

Also checked independently:
- `codekiln verify-isolated` (hardened container): the reference patch scores 1.0, an empty diff 0.0.
- `red_team/<package>.json`: four cheat attempts per package (edit the tests, read hidden files, hardcode, tamper with the verifier), run as real Harbor trials. All 20 scored 0.0, so no package is blocked.
- `variation_matrix.json`: each package's tests run against every other package's reference solution. A perfect diagonal (1.0 on it, 0.0 off it): the five tasks ask for different behaviour.
- `factory_report.html`: the offline dashboard (`codekiln report`), built from the run's ledger: funnel, matrix heatmap, per-task validity checklist, and the three rejected candidates with their reasons.
