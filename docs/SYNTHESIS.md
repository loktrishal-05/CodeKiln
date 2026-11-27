# Synthesis contract

The synthesizer's whole job is to turn one `Plan` into one `FeatureSpec`. Everything downstream
(packager, 3-state gate, mutation probes, dashboard) reads the artifacts that come out of it, so
the shape is fixed here before the LLM router lands. This page is the contract Cholan and Harsha
wire against; `src/codekiln/synthesis/models.py` is the executable version.

Status: **complete for the [S] items** — contract, RTM gate, package emitter, provider router, the
pipeline stage (`DIVERSITY_OK -> BLIND_READER_OK`), the dry-run instruction linter, four-class
mutation probes, the cost ledger with a hard budget breaker / idempotent resume, and the blind
eval runner with `eval_receipt.json` are in.

## Real-model run (six planner slots)

The planner seeds one slot per click subsystem (types, parser/options, formatting/help, shell
completion, testing/CliRunner, termui), so `codekiln run` defaults to six slots. On the host, with
the key supplied only through the environment:

```bash
export CODEKILN_LLM_API_KEY=...        # host only, never passed into a container
codekiln run https://github.com/pallets/click --slots 6 --workdir workspaces
codekiln synth RUN_ID --max-budget-usd 5.0
codekiln resume RUN_ID                 # only if the breaker stopped the first pass
```

Every completion goes through `Budget.meter_post`, so `billing_ledger.csv` records one
`PREAUTH_HOLD` + `PREAUTH_RELEASE` + `LLM_DEBIT` per provider call plus the idempotent
`BUDGET_ALLOCATION`. A spec is written only after `validate`, the RTM audit and the blind-reader
linter pass; execution correctness is still decided later by Harbor in the 3-state gate.
`tests/unit/test_six_slot_synthesis.py` exercises this exact path with the HTTP call faked and
asserts all six packages plus the CSV events.

## Test Author + Test Critic (#45)

`codekiln test-critic PACKAGE RUN_ID --alternatives N` asks the model for N *different but correct*
patches and executes each one in a copied Harbor task. A malformed or copied patch is rejected
before the trial; a correct alternative scoring below 1.0 is recorded in `test_critic.json` as
`too_narrow` with its exact reward. The model never grades the tests — the verifier does.

## What consumes what

| Consumer | Reads | From |
|---|---|---|
| Packager (`packaging/harbor.py`) | instruction, reference diff, feature tests, F2P ids | `FeatureSpec` / task dir |
| 3-state gate (`validation/gate_runner.py`) | `tests/fail_to_pass.json` | task dir |
| RTM gate | `rtm_map.json` | task dir root |
| Dashboard (`evaluation/report_builder.py`) | `mutation_kill_rate`, `nop_rewards`, `oracle_rewards` | ledger |
| Mutation probes (`validation/mutation_probe.py`) | `solution/reference.diff` with a swapped `solution/` | task dir -> `mutation_dossier.json` |

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
| `synthesis/runner.py` | `DIVERSITY_OK -> BLIND_READER_OK` / `REJECTED`, spec hash, dead-letter |
| `planner/models.py` | `dump_plan` / `load_plan` for fresh-process resume |

## Provider

Any OpenAI-compatible `/chat/completions` endpoint works. Credentials are host-only
(`CODEKILN_LLM_BASE_URL`, `CODEKILN_LLM_API_KEY`, `CODEKILN_LLM_MODEL`) and never reach a
container. The writer validates and RTM-checks every reply and retries at most twice with the
rejection reason appended; it never records its own success.

## Cost control

`synth` / `resume --run-id` meter every provider call into `billing_ledger.csv`:
`BUDGET_ALLOCATION` (once per run, idempotent), `PREAUTH_HOLD` (estimated before the call),
`PREAUTH_RELEASE` + `LLM_DEBIT` (real usage from the response). `--max-budget-usd` is a hard stop:
the writer never gets another call once the cap is reached; unfinished candidates stay
`DIVERSITY_OK` and `resume` skips finished/dead-lettered slots. Keys stay host-only.

## Blind eval

`codekiln eval packages/* --model claude-code --model codex` runs two or more real Harbor agents
on each task (agent sees `instruction.md` only), then writes `eval_receipt.json`: per-task rewards,
resolved flags, bloat ratio and blast radius from the agent diff, ATIF trace path, USD/solve, a
model x task solve matrix and tasks every model solved or none solved. Rows are mirrored into
`eval_runs`, which is what the dashboard's leaderboard reads.

## Team asks

Posted as [issue #31](https://github.com/loktrishal-05/CodeKiln/issues/31) (originally asked in #22):

- **Cholan:** is `rtm_map.json` at the task root OK for the packager's manifest, or should it live
  under `tests/`? Default is root so `tests/` stays exactly the grading surface.
- **Harsha:** confirm the fields the dashboard needs beyond `mutation_kill_rate`, `nop_rewards`,
  `oracle_rewards`, `model_id`, `resolved`. Issue #31 lists the exact schemas emitted today. The
  report now reads the kill rate from `mutation_dossier.json` and rewards from
  `generation_receipt.gates` when the receipt has no top-level keys.
- **Loktrishal:** resolved — planner output is enough; plans are persisted on `task_plans.plan_json`.
