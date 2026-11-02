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
"""


def connect(s: Settings) -> sqlite3.Connection:
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
