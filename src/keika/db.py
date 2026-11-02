"""SQLite の接続と表の定義。保存先は1か所（同期フォルダには置かない）。"""
from __future__ import annotations

import sqlite3

from keika.settings import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS office (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kana TEXT NOT NULL DEFAULT '',
  cm TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id TEXT, name TEXT NOT NULL,
  kana TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS doc_facts (
  sha256 TEXT PRIMARY KEY, rel_path TEXT NOT NULL, facts TEXT NOT NULL, read_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL, event_date TEXT NOT NULL, event_time TEXT NOT NULL DEFAULT '',
  kind_written TEXT NOT NULL DEFAULT '', kind_read TEXT, monitoring INTEGER, partner TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
  first_written_at TEXT,  -- CSV 由来は NULL（「不明（取り込み）」と表示する）
  source TEXT NOT NULL, source_key TEXT UNIQUE, unchecked INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS record_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id INTEGER NOT NULL, version INTEGER NOT NULL, snapshot TEXT NOT NULL,
  editor TEXT NOT NULL, edited_at TEXT NOT NULL, UNIQUE (record_id, version)
);
CREATE TABLE IF NOT EXISTS imported_files (
  sha256 TEXT PRIMARY KEY, rel_path TEXT NOT NULL, rows INTEGER NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS check_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, run_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finding_responses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, user_id TEXT, kind TEXT NOT NULL, record_id INTEGER,
  evidence_hash TEXT NOT NULL, run_id INTEGER NOT NULL, who TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, finding TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, original TEXT NOT NULL, source TEXT NOT NULL, entry TEXT NOT NULL,
  warnings TEXT NOT NULL, note TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_files (
  sha256 TEXT PRIMARY KEY, rel_path TEXT NOT NULL, taken_at TEXT NOT NULL
);
-- 履歴は消せない。記録も消せない。記入日時は変えられない。
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON record_versions
BEGIN SELECT RAISE(ABORT, '履歴は消せません'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON record_versions
BEGIN SELECT RAISE(ABORT, '履歴は変えられません'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, '記録は消せません'); END;
CREATE TRIGGER IF NOT EXISTS records_written_at_fixed BEFORE UPDATE OF first_written_at ON records
WHEN OLD.first_written_at IS NOT NEW.first_written_at
BEGIN SELECT RAISE(ABORT, '記入日時は変えられません'); END;
"""


def connect(s: Settings) -> sqlite3.Connection:
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
