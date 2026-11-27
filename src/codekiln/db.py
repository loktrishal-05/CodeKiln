"""SQLite WAL ledger: six tables giving lineage from commit SHA to eval result."""
import hashlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
  id INTEGER PRIMARY KEY, url TEXT, base_commit TEXT NOT NULL, stack TEXT, image_digest TEXT,
  UNIQUE (url, base_commit));
CREATE TABLE IF NOT EXISTS factory_runs (
  id INTEGER PRIMARY KEY, repo_id INTEGER REFERENCES repositories(id), mode TEXT,
  slots INTEGER, accepted INTEGER DEFAULT 0, total_usd REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS task_candidates (
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES factory_runs(id),
  idempotency_key TEXT UNIQUE NOT NULL, category TEXT, difficulty TEXT, ast_fingerprint TEXT,
  status TEXT NOT NULL, rejection_reason TEXT, package_sha256 TEXT);
CREATE TABLE IF NOT EXISTS task_plans (
  candidate_id INTEGER PRIMARY KEY REFERENCES task_candidates(id),
  plan_json TEXT NOT NULL, attempts INTEGER DEFAULT 0, spec_sha256 TEXT, last_error TEXT,
  updated_at TEXT, package_path TEXT);
CREATE TABLE IF NOT EXISTS validation_executions (
  id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES task_candidates(id),
  phase TEXT NOT NULL, run_index INTEGER, exit_code INTEGER, f2p_passed INTEGER,
  p2p_passed INTEGER, ctrf_path TEXT);
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES task_candidates(id),
  model_id TEXT, resolved INTEGER, bloat_ratio REAL, blast_radius INTEGER, atif_path TEXT);
"""


def idempotency_key(repo: str, commit: str, mode: str, slot: int) -> str:
    return hashlib.sha256(f"{repo}|{commit}|{mode}|{slot}".encode()).hexdigest()


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    # `connect` is also called on ledgers made before a new column existed.
    if "package_path" not in {row[1] for row in con.execute("PRAGMA table_info(task_plans)")}:
        con.execute("ALTER TABLE task_plans ADD COLUMN package_path TEXT")
    return con
