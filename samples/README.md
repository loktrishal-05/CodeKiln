# Samples: the five validated click packages

Real output of `codekiln factory` on click 8.5.1 (pinned commit `cf19179e8e2f`), written by Groq `openai/gpt-oss-120b`
and accepted only by execution: Harbor States A/B/C (per-test), the mutation probes, the scrubber, a sealed manifest.

| Package | Repairs used | Mutants killed |
|---|---|---|
| click-completion-enabled | 0 | 100% |
| click-parse-bool | 2 | 100% |
| click-parse-json-param | 2 | 100% |
| click-prompt-int-range | 0 | 100% |
| click-result-contains | 0 | 100% |

Each folder is a Harbor task (`instruction.md`, `environment/`, `tests/`, `solution/`, `task.toml`) (sealed and unmodified) plus the evidence:
`mutation_dossier.json`, `generation_receipt.json`, `manifest.sha256`, and `samples/harbor_logs/<package>/nop.json` (no change: reward 0.0, the
feature tests fail) and `samples/harbor_logs/<package>/oracle.json` (reference patch: reward 1.0, every test passes).

Also checked independently with `codekiln verify-isolated` (hardened container): the reference patch scores 1.0, an empty diff 0.0.
