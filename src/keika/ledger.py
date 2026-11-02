"""台帳: 利用者・事業所・関係者。名前の補正と振り分けの土台。"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from keika.settings import Settings, safe_path

USER_DIR = "利用者"
_ID_PREFIX = re.compile(r"^[A-Za-z]+\d+_")


@dataclass(frozen=True)
class User:
    id: str
    name: str
    kana: str = ""
    cm: str = ""
    data: dict = field(default_factory=dict)  # 要介護度、認定期間、種別など


@dataclass(frozen=True)
class Match:
    kind: str  # user / staff / family / doctor / hospital / provider / houkatsu
    id: str
    name: str
    user_id: str | None


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _to_user(row: sqlite3.Row) -> User:
    return User(row["id"], row["name"], row["kana"], row["cm"], json.loads(row["data"]))


def upsert_user(conn: sqlite3.Connection, user: User) -> None:
    conn.execute(
        "INSERT INTO users (id, name, kana, cm, data) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, kana=excluded.kana, cm=excluded.cm, data=excluded.data",
        (user.id, user.name, user.kana, user.cm, json.dumps(user.data, ensure_ascii=False)),
    )
    conn.commit()


def upsert_person(conn, pid: str, kind: str, user_id: str | None, name: str, kana: str, data: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO people (id, kind, user_id, name, kana, data) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, kind, user_id, name, kana, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()


def seed_from_json(conn: sqlite3.Connection, path: Path) -> None:
    src = json.loads(Path(path).read_text(encoding="utf-8"))
    office = src.get("office") or {}
    conn.execute(
        "INSERT OR REPLACE INTO office (id, data) VALUES (?, ?)",
        (office.get("id", "OF"), json.dumps(office, ensure_ascii=False)),
    )
    for st in office.get("staff", []):
        upsert_person(conn, st["id"], "staff", None, st["name"], st.get("kana", ""), st)
    for u in src.get("users", []):
        extra = {k: v for k, v in u.items() if k not in ("id", "name", "kana", "cm")}
        upsert_user(conn, User(u["id"], u["name"], u.get("kana", ""), u.get("cm", ""), extra))
    for f in src.get("family", []):
        upsert_person(conn, f["id"], "family", f.get("user"), f["name"], f.get("kana", ""), f)
    for key, kind in (("doctors", "doctor"), ("hospitals", "hospital"), ("providers", "provider"), ("houkatsu", "houkatsu")):
        for p in src.get(key, []):
            upsert_person(conn, p["id"], kind, None, p["name"], p.get("kana", ""), p)
    conn.commit()


def list_users(conn: sqlite3.Connection, cm: str | None = None) -> list[User]:
    if cm:
        rows = conn.execute("SELECT * FROM users WHERE cm = ? ORDER BY id", (cm,))
    else:
        rows = conn.execute("SELECT * FROM users ORDER BY id")
    return [_to_user(r) for r in rows]


def get_user(conn: sqlite3.Connection, user_id: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _to_user(row) if row else None


def list_people(conn: sqlite3.Connection, kind: str | None = None) -> list[Match]:
    rows = conn.execute("SELECT * FROM people ORDER BY id").fetchall()
    return [Match(r["kind"], r["id"], r["name"], r["user_id"]) for r in rows if kind in (None, r["kind"])]


def find_person(conn: sqlite3.Connection, text: str) -> list[Match]:
    """氏名の一部か読みが含まれる人を全部返す。同じ名前の人は絞らない（迷いは要確認箱で人が決める）。"""
    needle = squash(text)
    if not needle:
        return []
    found = []
    for r in conn.execute("SELECT id, name, kana FROM users ORDER BY id"):
        if needle in squash(r["name"]) or (r["kana"] and needle in squash(r["kana"])):
            found.append(Match("user", r["id"], r["name"], r["id"]))
    for r in conn.execute("SELECT id, kind, user_id, name, kana FROM people ORDER BY id"):
        if needle in squash(r["name"]) or (r["kana"] and needle in squash(r["kana"])):
            found.append(Match(r["kind"], r["id"], r["name"], r["user_id"]))
    return found


def user_folder(conn: sqlite3.Connection, s: Settings, user_id: str) -> Path | None:
    """利用者のフォルダを氏名で探す。ID の付いていない氏名だけのフォルダでも見つかる。"""
    user = get_user(conn, user_id)
    base = safe_path(s, USER_DIR)
    if user is None or not base.is_dir():
        return None
    target = squash(user.name)
    for child in sorted(base.iterdir()):
        if child.is_dir() and squash(_ID_PREFIX.sub("", child.name)) == target:
            return child
    return None
