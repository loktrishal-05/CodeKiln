#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/pallets/click"
SLOTS=6  # one per click subsystem; spare slots absorb gate rejections on the way to 5 packages
RUNS=3
MAX_BUDGET_USD="5.00"
OUT_DIR="runs/demo"
MODEL="${CODEKILN_LLM_MODEL:-gpt-4o-mini}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: scripts/demo_run.sh [OPTIONS]

Run the CodeKiln pipeline end-to-end and emit Harbor packages + HTML report.

Options:
  --repo URL_OR_PATH      Target git URL, dir, or zip (default: https://github.com/pallets/click)
  --slots N               Number of candidate task slots (default: 6)
  --runs N                3-state gate repeat count (default: 3)
  --max-budget-usd USD    Hard LLM budget breaker in USD (default: 5.00)
  --model NAME            Model name recorded on receipts (default: CODEKILN_LLM_MODEL or gpt-4o-mini)
  --out DIR               Output directory for run artifacts (default: runs/demo)
  --dry-run               Run ingestion + planning + diversity only (no LLM key or Docker needed)
  -h, --help              Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --slots) SLOTS="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --max-budget-usd) MAX_BUDGET_USD="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || command -v python)"
  fi
fi

CODEKILN_BIN="${CODEKILN_BIN:-}"
if [[ -z "$CODEKILN_BIN" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/codekiln" ]]; then
    CODEKILN_BIN="$ROOT_DIR/.venv/bin/codekiln"
  elif command -v codekiln >/dev/null 2>&1; then
    CODEKILN_BIN="$(command -v codekiln)"
  else
    echo "error: codekiln CLI not found (activate .venv or install with pip install -e .)" >&2
    exit 127
  fi
fi

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
DB_PATH="$OUT_DIR/codekiln.sqlite"
WORKSPACES_DIR="$OUT_DIR/workspaces"
STAGING_DIR="$OUT_DIR/staging"
PACKAGES_DIR="$OUT_DIR/packages"
VERIFY_DIR="$OUT_DIR/verify"
PROBES_DIR="$OUT_DIR/probes"
MATRIX_DIR="$OUT_DIR/matrix"
MATRIX_JSON="$OUT_DIR/variation_matrix.json"
LEDGER_CSV="$OUT_DIR/billing_ledger.csv"
REPORT_HTML="$OUT_DIR/factory_report.html"

export CODEKILN_DB_PATH="$DB_PATH"
export CODEKILN_BILLING_LEDGER="$LEDGER_CSV"

if [[ "$DRY_RUN" -eq 0 ]]; then
  command -v harbor >/dev/null 2>&1 || { echo "error: harbor CLI not found on PATH" >&2; exit 127; }
  command -v docker >/dev/null 2>&1 || { echo "error: docker CLI not found on PATH" >&2; exit 127; }
  docker info >/dev/null 2>&1 || { echo "error: Docker daemon is not reachable" >&2; exit 1; }
  "$PYTHON_BIN" -c '
import sys
from codekiln.config import Settings
if not Settings().llm_api_key:
    print("error: CODEKILN_LLM_API_KEY is empty (set it in env or .env)", file=sys.stderr)
    sys.exit(2)
'
fi

echo "==> [1/6] Ingest & plan ($REPO, slots=$SLOTS)"
"$CODEKILN_BIN" run "$REPO" --slots "$SLOTS" --max-budget-usd "$MAX_BUDGET_USD" --workdir "$WORKSPACES_DIR"

RUN_ID="$("$PYTHON_BIN" -c '
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("SELECT MAX(id) FROM factory_runs").fetchone()
print(row[0] if row and row[0] is not None else "")
' "$DB_PATH")"

if [[ -z "$RUN_ID" ]]; then
  echo "error: no factory_runs row created in $DB_PATH" >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  CANDIDATES="$("$PYTHON_BIN" -c '
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print(con.execute("SELECT COUNT(*) FROM task_candidates WHERE run_id = ?", (int(sys.argv[2]),)).fetchone()[0])
' "$DB_PATH" "$RUN_ID")"
  echo "==> Dry run complete: run_id=$RUN_ID, candidates=$CANDIDATES, db=$DB_PATH"
  exit 0
fi

echo "==> [2/6] Synthesize candidates (run_id=$RUN_ID, budget=\$$MAX_BUDGET_USD)"
# synth exits 1 on a budget stop or rejections even when some candidates were staged;
# keep going with what was staged (the gate loop below fails if nothing was).
if ! "$CODEKILN_BIN" synth "$RUN_ID" \
  --db "$DB_PATH" \
  --packages "$STAGING_DIR" \
  --model "$MODEL" \
  --max-budget-usd "$MAX_BUDGET_USD" \
  --ledger-csv "$LEDGER_CSV"; then
  echo "warn: synth stopped early (budget cap or rejections); continuing with staged candidates" >&2
fi

mkdir -p "$PACKAGES_DIR"
PROMOTED=()

echo "==> [3/6] Gate, mutation probes, security scrub, and atomic packaging"
shopt -s nullglob
for task_dir in "$STAGING_DIR"/*; do
  [[ -d "$task_dir" ]] || continue
  task_name="$(basename "$task_dir")"
  echo "--- Processing candidate: $task_name"
  if ! "$CODEKILN_BIN" blind-read "$task_dir"; then
    echo "warn: blind-read rejected $task_name; skipping" >&2
    continue
  fi
  if ! "$CODEKILN_BIN" verify "$task_dir" --work "$VERIFY_DIR" --db "$DB_PATH" --runs "$RUNS"; then
    echo "warn: verify gate failed for $task_name; skipping" >&2
    continue
  fi
  if ! "$CODEKILN_BIN" probe "$task_dir" --work "$PROBES_DIR"; then
    echo "warn: mutation probes failed for $task_name; skipping" >&2
    continue
  fi
  if ! "$CODEKILN_BIN" scrub "$task_dir"; then
    echo "warn: security scrub blocked $task_name; skipping" >&2
    continue
  fi
  if "$CODEKILN_BIN" package "$task_dir" "$PACKAGES_DIR/$task_name" \
      --gates-ok --run-id "$RUN_ID" --db "$DB_PATH" --model "$MODEL"; then
    PROMOTED+=("$PACKAGES_DIR/$task_name")
  else
    echo "warn: packaging failed for $task_name; skipping" >&2
  fi
done
shopt -u nullglob

if [[ "${#PROMOTED[@]}" -eq 0 ]]; then
  echo "error: no staged tasks passed all gates and promoted to $PACKAGES_DIR" >&2
  exit 1
fi

echo "==> [4/6] Promoted ${#PROMOTED[@]} package(s) to $PACKAGES_DIR"
if [[ "${#PROMOTED[@]}" -ge 2 ]]; then
  echo "==> [5/6] Cross-task oracle variation matrix"
  "$CODEKILN_BIN" matrix "${PROMOTED[@]}" --out "$MATRIX_JSON" --work "$MATRIX_DIR" || true
else
  echo "==> [5/6] Skipping variation matrix (need >= 2 promoted packages)"
fi

echo "==> [6/6] Static HTML report -> $REPORT_HTML"
"$CODEKILN_BIN" report "$RUN_ID" \
  --db "$DB_PATH" \
  --packages "$PACKAGES_DIR" \
  --matrix "$MATRIX_JSON" \
  --out "$REPORT_HTML"

echo "Done! Artifacts in $OUT_DIR (db=$DB_PATH, packages=$PACKAGES_DIR, report=$REPORT_HTML)"

