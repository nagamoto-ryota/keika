"""開くと終わっている入口。記録できた事を先に言い、聞く事だけを1日の上限つきで出す。

点検の結果と項目の数は変えない。毎日の画面に出す物を選ぶだけで、全部は「すべての項目」で見られる。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from keika.ledger import get_user
from keika.settings import Settings, now_real, today
from keika.tasks import Board

DAILY_ASKS = 5
SOON_DAYS = 30
MAX_DEADLINES = 3
MAX_NEW_ROWS = 5
NEW_VISIT_MINUTES = 30  # これより間が空いたら、開き直したとみなす


@dataclass
class NewRecord:
    id: int
    user_id: str
    user_name: str
    event_date: str
    kind: str


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO desk_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def opened_since(conn: sqlite3.Connection, *, write: bool = True) -> str | None:
    """「前回開いた時」。続けて開いている間は同じ時刻を返す（更新しても一覧が消えないように）。"""
    now = now_real()
    last, before = _get(conn, "home_last_open"), _get(conn, "home_before")
    if write:
        if last is None or now - datetime.fromisoformat(last) > timedelta(minutes=NEW_VISIT_MINUTES):
            before = last
            _set(conn, "home_before", before or "")
        _set(conn, "home_last_open", now.isoformat())
        conn.commit()
    return before or None


def new_records(conn: sqlite3.Connection, since: str | None, user_ids: set[str] | None = None) -> list[NewRecord]:
    """話から記録した物。取り込んだ CSV は数えない。"""
    rows = conn.execute("SELECT id, user_id, event_date, kind_read, kind_written, first_written_at FROM records WHERE first_written_at IS NOT NULL AND source NOT LIKE 'csv:%' ORDER BY id DESC").fetchall()
    found = []
    for r in rows:
        if (since is None or r["first_written_at"] > since) and (user_ids is None or r["user_id"] in user_ids):
            user = get_user(conn, r["user_id"])
            found.append(NewRecord(r["id"], r["user_id"], user.name if user else r["user_id"], r["event_date"], r["kind_read"] or r["kind_written"] or "記録"))
    return found


def pick_asks(conn: sqlite3.Connection, s: Settings, keys: list[str], *, write: bool = True) -> tuple[list[str], int]:
    """今日聞く物と、あと何件か。今日すでに出した分も上限に数える。"""
    day = today(s).isoformat()
    shown = [r["key"] for r in conn.execute("SELECT key FROM home_asks WHERE day = ?", (day,))]
    picked = [k for k in keys if k in shown]
    room = DAILY_ASKS - len(shown)
    for key in keys:
        if room <= 0:
            break
        if key not in shown:
            picked.append(key)
            room -= 1
            if write:
                conn.execute("INSERT OR IGNORE INTO home_asks (day, key) VALUES (?, ?)", (day, key))
    if write:
        conn.commit()
    return [k for k in keys if k in picked], len(keys) - len(picked)


def deadline_lines(board: Board, s: Settings) -> list[dict]:
    """期限の近い物だけ、秘書の一言にする。1人1行。"""
    on = today(s)
    best: dict[str, dict] = {}
    for person in board.people:
        for task in person.tasks:
            if not task.deadline:
                continue
            try:
                when = date.fromisoformat(task.deadline)
            except ValueError:
                continue
            left = (when - on).days
            if left <= SOON_DAYS and (person.user.id not in best or left < best[person.user.id]["days"]):
                best[person.user.id] = {"user_id": person.user.id, "name": person.user.name, "title": task.title, "deadline": task.deadline, "days": left}
    return sorted(best.values(), key=lambda x: x["days"])[:MAX_DEADLINES]


def unchecked_count(conn: sqlite3.Connection, user_ids: set[str] | None = None) -> int:
    rows = conn.execute("SELECT user_id FROM records WHERE unchecked = 1 AND source NOT LIKE 'csv:%'").fetchall()
    return sum(user_ids is None or r["user_id"] in user_ids for r in rows)
