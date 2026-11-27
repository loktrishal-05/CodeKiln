# Synthesis contract

The synthesizer's whole job is to turn one `Plan` into one `FeatureSpec`. Everything downstream
(packager, 3-state gate, mutation probes, dashboard) reads the artifacts that come out of it, so
the shape is fixed here before the LLM router lands. This page is the contract Cholan and Harsha
wire against; `src/codekiln/synthesis/models.py` is the executable version.

Status: **synthesis wired** — contract, RTM gate, package emitter, provider router and the pipeline
stage (`DIVERSITY_OK -> SYNTHESIZED`) are in. Blind-reader linter, mutation probes and the eval
runner are next.

## What consumes what

| Consumer | Reads | From |
|---|---|---|
| Packager (`packaging/harbor.py`) | instruction, reference diff, feature tests, F2P ids | `FeatureSpec` / task dir |
| 3-state gate (`validation/gate_runner.py`) | `tests/fail_to_pass.json` | task dir |
| RTM gate | `rtm_map.json` | task dir root |
| Dashboard (`evaluation/report_builder.py`) | `mutation_kill_rate`, `nop_rewards`, `oracle_rewards` | ledger |
| Mutation probes (next) | `solution/reference.diff` with a swapped `solution/` | task dir |

## The object

One JSON object per candidate. `requirements` is the only place where a REQ id is tied to tests,
so the RTM audit has no convention to guess (no markers, no name prefixes).

```json
{
  "task": "click-echo-uppercase",
  "category": "api_cli",
  "difficulty": "L2",
  "instruction": "# Add uppercase output to `echo`\n\n## Requirements\n- REQ-1: `echo` accepts `uppercase: bool = False` ...\n- REQ-2: `CliRunner` captures the uppercased output ...\n\n## Errors\n- `TypeError` when `uppercase` is not a bool.\n",
  "reference_diff": "diff --git a/src/click/utils.py b/src/click/utils.py\n--- a/src/click/utils.py\n+++ b/src/click/utils.py\n@@ ...\n",
  "feature_tests": {
    "test_echo_uppercase.py": "from click import echo\nfrom click.testing import CliRunner\n\ndef test_req1_uppercases():\n    ...\n"
  },
  "f2p": [
    "tests/test_echo_uppercase.py::test_req1_uppercases",
    "tests/test_echo_uppercase.py::test_req2_runner"
  ],
  "requirements": [
    {"id": "REQ-1", "statement": "echo(..., uppercase=True) upper-cases the message", "tests": ["tests/test_echo_uppercase.py::test_req1_uppercases"]},
    {"id": "REQ-2", "statement": "CliRunner sees the uppercased output and exit code 0", "tests": ["tests/test_echo_uppercase.py::test_req2_runner"]}
  ]
}
```

Notes:

- `f2p` are pytest node ids exactly as pytest prints them; parametrised ids may carry `[...]`.
- `p2p` is **not** here: the gate computes it inside the task container (State A) and the packager
  writes `tests/pass_to_pass.json`.
- `feature_tests` keys are file names only (`test_x.py`), not paths; they land in `tests/feature/`.

## Rules enforced before any file is written

`validate()` then `rtm.require_ok()` — a rejected spec writes nothing:

- task is kebab-case; feature test names match `test_[a-z0-9_]+\.py`; difficulty is L1/L2/L3.
- at least one requirement, unique `REQ-n` ids, every id declared in `instruction.md`, no orphans.
- `reference_diff` starts with `diff --git `, adds lines, and deletes no files.
  Stripping is really caught by execution: State A/B run every P2P test, so removing behaviour
  cannot pass. The lint only blocks the obvious move.
- every REQ has ≥1 test; every mapped test is in `f2p` **and** is a function the feature test file
  actually defines (AST, not string matching).
- every symbol the tests `from click... import` is named somewhere in `instruction.md`
  (the spec/test interface linter from FR-009).

## RTM map

Written to `<task>/rtm_map.json`:

```json
{
  "task": "click-echo-uppercase",
  "ok": true,
  "requirements": {"REQ-1": ["tests/test_echo_uppercase.py::test_req1_uppercases"]},
  "problems": []
}
```

Gate: `RTM_OK` only when `ok` is true. `spec-check` and `write_package` enforce it today;
`pipeline.py` wiring is the next PR.

## CLI

```console
codekiln spec-check path/to/spec.json          # writes rtm_map.json next to it
codekiln spec-check path/to/spec.json --out x.json
codekiln synth RUN_ID --packages packages      # writes one Harbor package per candidate
```

`spec-check` exits 1 printing every problem. `synth` needs a host-only
`CODEKILN_LLM_API_KEY`; it reuses plans persisted on `task_plans`, skips candidates already past
`DIVERSITY_OK`, retries failures up to three runs, then dead-letters them with a reason.

## Module map

| File | Role |
|---|---|
| `synthesis/models.py` | `FeatureSpec`, `Requirement`, `validate`, JSON load/save |
| `synthesis/rtm.py` | `audit` -> `RtmMap`, `require_ok`, `write_map` |
| `synthesis/emit.py` | `write_package`: validate + RTM + `packaging.harbor.write_task` |
| `synthesis/spec_generator.py` | deterministic contract-first prompt for the writer |
| `synthesis/llm_router.py` | OpenAI-compatible client, JSON parsing, bounded repair |
| `synthesis/runner.py` | `DIVERSITY_OK -> SYNTHESIZED` / `REJECTED`, spec hash, dead-letter |
| `planner/models.py` | `dump_plan` / `load_plan` for fresh-process resume |

## Provider

Any OpenAI-compatible `/chat/completions` endpoint works. Credentials are host-only
(`CODEKILN_LLM_BASE_URL`, `CODEKILN_LLM_API_KEY`, `CODEKILN_LLM_MODEL`) and never reach a
container. The writer validates and RTM-checks every reply and retries at most twice with the
rejection reason appended; it never records its own success.

## Next (in order)

1. Blind-reader linter: dry-run check on the agent-visible instruction, then `BLIND_READER_OK`.
2. Mutation probes: four classes, each a Harbor trial with a swapped `solution/`, >=80% semantic kill.
3. Cost ledger, budget breaker and idempotent resume.
4. Blind eval runner + `eval_receipt.json` (2+ models).

## Team asks

- **Cholan:** is `rtm_map.json` at the task root OK for the packager's manifest, or should it live
  under `tests/`? I default to root so `tests/` stays exactly the grading surface.
- **Harsha:** the three JSON schemas are `rtm_map.json` (here), `mutation_dossier.json` and
  `eval_receipt.json` (next two PRs). Tell me the fields the dashboard needs beyond
  `mutation_kill_rate`, `nop_rewards`, `oracle_rewards`, `model_id`, `resolved`.
- **Loktrishal:** planner output is enough — no change requested. If you want per-slot prompts
  reproducible, I can persist the `Plan` (files/symbols/fanout) on `task_candidates`; today only
  category/difficulty/fingerprint are stored.
