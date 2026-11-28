#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/pallets/click"
COMMIT="cf19179e8e2fd54c601378cec9e0d21bb12b6fb1"  # click 8.5.1: the pinned base commit every task is built on
SLOTS=8  # a real run packaged 5 of 8; spare slots absorb gate rejections on the way to 5 packages
RUNS=3
MAX_BUDGET_USD="5.00"
OUT_DIR="runs/demo"
MODEL="${CODEKILN_LLM_MODEL:-gpt-4o-mini}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: scripts/demo_run.sh [OPTIONS]

Run the CodeKiln pipeline end-to-end and emit Harbor packages + HTML report.
`codekiln factory` drives every stage from the ledger: synthesize, Harbor 3-state gate,
mutation probes, evidence-based repair of rejected candidates, scrub, package.

Options:
  --repo URL_OR_PATH      Target git URL, dir, or zip (default: https://github.com/pallets/click)
  --commit SHA            Pin the base commit (default: click 8.5.1, cf19179e8e2f...)
  --slots N               Number of candidate task slots (default: 8)
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
    --commit) COMMIT="$2"; shift 2 ;;
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
WORK_DIR="$OUT_DIR/work"
PACKAGES_DIR="$OUT_DIR/packages"
MATRIX_DIR="$OUT_DIR/matrix"
MATRIX_JSON="$OUT_DIR/variation_matrix.json"
LEDGER_CSV="$OUT_DIR/billing_ledger.csv"
REPORT_HTML="$OUT_DIR/factory_report.html"

export CODEKILN_DB_PATH="$DB_PATH"
export CODEKILN_BILLING_LEDGER="$LEDGER_CSV"

latest_run_id() {
  "$PYTHON_BIN" -c '
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("SELECT MAX(id) FROM factory_runs").fetchone()
print(row[0] if row and row[0] is not None else "")
' "$DB_PATH"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> Ingest & plan only ($REPO, slots=$SLOTS)"
  "$CODEKILN_BIN" run "$REPO" --slots "$SLOTS" --max-budget-usd "$MAX_BUDGET_USD" --workdir "$WORKSPACES_DIR"
  RUN_ID="$(latest_run_id)"
  if [[ -z "$RUN_ID" ]]; then
    echo "error: no factory_runs row created in $DB_PATH" >&2
    exit 1
  fi
  CANDIDATES="$("$PYTHON_BIN" -c '
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print(con.execute("SELECT COUNT(*) FROM task_candidates WHERE run_id = ?", (int(sys.argv[2]),)).fetchone()[0])
' "$DB_PATH" "$RUN_ID")"
  echo "==> Dry run complete: run_id=$RUN_ID, candidates=$CANDIDATES, db=$DB_PATH"
  exit 0
fi

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

echo "==> [1/3] Factory: plan, synthesize, gate, probe, repair, package ($REPO @ ${COMMIT:0:12}, slots=$SLOTS, budget=\$$MAX_BUDGET_USD)"
# factory exits 1 when any candidate was rejected, which is normal (5 of 8 packaged in a real run);
# success is judged by what was packaged, below.
"$CODEKILN_BIN" factory "$REPO" \
  --commit "$COMMIT" --slots "$SLOTS" --runs "$RUNS" \
  --workdir "$WORKSPACES_DIR" --work "$WORK_DIR" --dest "$PACKAGES_DIR" \
  --db "$DB_PATH" --model "$MODEL" --max-budget-usd "$MAX_BUDGET_USD" --ledger-csv "$LEDGER_CSV" || true

RUN_ID="$(latest_run_id)"
PROMOTED=()
shopt -s nullglob
for p in "$PACKAGES_DIR"/*/; do
  PROMOTED+=("${p%/}")
done
shopt -u nullglob

if [[ "${#PROMOTED[@]}" -eq 0 ]]; then
  echo "error: no candidate passed all gates and was packaged into $PACKAGES_DIR" >&2
  exit 1
fi
echo "==> Packaged ${#PROMOTED[@]} task(s) in $PACKAGES_DIR"

if [[ "${#PROMOTED[@]}" -ge 2 ]]; then
  echo "==> [2/3] Cross-task oracle variation matrix"
  "$CODEKILN_BIN" matrix "${PROMOTED[@]}" --out "$MATRIX_JSON" --work "$MATRIX_DIR" || true
else
  echo "==> [2/3] Skipping variation matrix (need >= 2 packaged tasks)"
fi

echo "==> [3/3] Static HTML report -> $REPORT_HTML"
"$CODEKILN_BIN" report "$RUN_ID" \
  --db "$DB_PATH" \
  --packages "$PACKAGES_DIR" \
  --matrix "$MATRIX_JSON" \
  --out "$REPORT_HTML"

echo "Done! Artifacts in $OUT_DIR (db=$DB_PATH, packages=$PACKAGES_DIR, report=$REPORT_HTML)"
