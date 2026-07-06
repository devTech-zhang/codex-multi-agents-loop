from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import artifact_root, db_path, log_root, memory_root, source_root


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  requirement TEXT NOT NULL,
  platform TEXT NOT NULL,
  source TEXT NOT NULL,
  owner_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  current_step TEXT NOT NULL,
  status TEXT NOT NULL,
  prd_version INTEGER DEFAULT 0,
  review_round INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS step_runs (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  category TEXT NOT NULL,
  executor TEXT NOT NULL,
  status TEXT NOT NULL,
  input_json TEXT,
  output_json TEXT,
  started_at TEXT,
  completed_at TEXT,
  UNIQUE(run_id, step_id)
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT,
  result_json TEXT,
  error TEXT,
  attempts INTEGER DEFAULT 0,
  claimed_agent TEXT,
  invocation_mode TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  path TEXT NOT NULL,
  version INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  status TEXT NOT NULL,
  schema_json TEXT NOT NULL,
  data_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, step_id)
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  payload_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_reviews (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  target_artifact_id TEXT,
  round INTEGER NOT NULL,
  reviewer_agent TEXT NOT NULL,
  opinion_path TEXT,
  summary TEXT,
  severity TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_memory (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  memory_path TEXT NOT NULL,
  last_summary TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, agent_name)
);

CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_name ON artifacts(run_id, name, version);
CREATE INDEX IF NOT EXISTS idx_reviews_run_round ON workflow_reviews(run_id, round);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"


def init_db() -> Path:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact_root().mkdir(parents=True, exist_ok=True)
    source_root().mkdir(parents=True, exist_ok=True)
    log_root().mkdir(parents=True, exist_ok=True)
    memory_root().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _migrate_schema(conn: sqlite3.Connection) -> None:
    _add_column(conn, "workflow_runs", "prd_version", "INTEGER DEFAULT 0")
    _add_column(conn, "workflow_runs", "review_round", "INTEGER DEFAULT 0")
    _add_column(conn, "jobs", "claimed_agent", "TEXT")
    _add_column(conn, "jobs", "invocation_mode", "TEXT")


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if column in {row[1] for row in rows}:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    init_db()
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def row_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
