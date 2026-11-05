"""メールに付ける書類。候補はその利用者の物だけ。最後に選ぶのは人。

候補は、その人の読み取りフォルダの書類と、ケイカで作った下書き・ケアプラン（Word にして付ける）。
他の人の書類は候補にも出さず、画面から送られてきても使わない。
"""
from __future__ import annotations

import mimetypes
import re
import sqlite3

from keika.care_plan import list_care_plans, to_docx as care_plan_docx
from keika.drafts import list_drafts, to_docx as draft_docx
from keika.ledger import get_user, list_people
from keika.machine_checks import user_refs
from keika.settings import Settings, now_real

MAX_BYTES = 25 * 1024 * 1024  # Gmail の上限
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SAYS_ATTACHED = re.compile(r"添付")


def _label(name: str) -> str:
    """本文で触れているかを見るための呼び名。「アセスメント_2025-11-20.pdf」なら「アセスメント」。"""
    return re.split(r"[_（(]|\.\w+$", name)[0].strip()


def _size_label(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)}KB"


def _word_items(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    user = get_user(conn, user_id)
    staff = {p.id: p.name for p in list_people(conn, "staff")}
    items = []
    for d in list_drafts(conn, user_id, status=None):
        data = draft_docx(d, user.name, staff.get(user.cm, ""))
        items.append({"key": f"d:{d.id}", "name": f"{d.title}_{d.event_date}.docx", "label": d.title, "mime": WORD, "size": len(data), "data": data})
    for p in list_care_plans(conn, user_id):
        data = care_plan_docx(p)
        items.append({"key": f"p:{p.id}", "name": f"{p.title}_下書き_{user.name}.docx", "label": p.title, "mime": WORD, "size": len(data), "data": data})
    return items


def candidates(conn: sqlite3.Connection, s: Settings, user_id: str) -> list[dict]:
    user = get_user(conn, user_id)
    if user is None:
        return []
    items = []
    for r in user_refs(conn, s, user):
        path = s.drive_root / r.rel_path
        if not path.is_file():
            continue
        name = r.rel_path.rsplit("/", 1)[-1]
        items.append({"key": f"f:{r.rel_path}", "name": name, "label": _label(name), "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                      "size": path.stat().st_size, "path": path})
    items += _word_items(conn, user_id)
    for it in items:
        it["size_label"] = _size_label(it["size"])
    return items


def files_for(conn: sqlite3.Connection, s: Settings, user_id: str, keys: list[str]) -> list[tuple[str, str, bytes]]:
    """選ばれた物の中身。その人の候補に無い物は黙って使わない。"""
    by_key = {it["key"]: it for it in candidates(conn, s, user_id)}
    out = []
    for key in keys:
        it = by_key.get(key)
        if it:
            out.append((it["name"], it["mime"], it["data"] if "data" in it else it["path"].read_bytes()))
    return out


def choose(conn: sqlite3.Connection, mail_id: int, keys: list[str]) -> None:
    """人が選んだ物を残す。一度でも選んだら、本文から推す既定は使わない。"""
    conn.execute("DELETE FROM mail_attach WHERE mail_id = ?", (mail_id,))
    conn.executemany("INSERT OR IGNORE INTO mail_attach (mail_id, key) VALUES (?, ?)", [(mail_id, k) for k in keys])
    conn.execute("INSERT OR REPLACE INTO mail_attach_set (mail_id, at) VALUES (?, ?)", (mail_id, now_real().isoformat()))
    conn.commit()


def chosen(conn: sqlite3.Connection, s: Settings, mail_id: int, user_id: str, body: str, items: list[dict] | None = None) -> list[str]:
    items = candidates(conn, s, user_id) if items is None else items
    known = [it["key"] for it in items]
    if conn.execute("SELECT 1 FROM mail_attach_set WHERE mail_id = ?", (mail_id,)).fetchone():
        picked = {r["key"] for r in conn.execute("SELECT key FROM mail_attach WHERE mail_id = ?", (mail_id,))}
        return [k for k in known if k in picked]
    if not SAYS_ATTACHED.search(body):
        return []
    return [it["key"] for it in items if it["label"] and it["label"] in body]  # AI の案で触れている書類だけ、最初から選んでおく


def keep_names(conn: sqlite3.Connection, mail_id: int, names: list[str]) -> None:
    conn.executemany("INSERT INTO mail_sent_attach (mail_id, name) VALUES (?, ?)", [(mail_id, n) for n in names])


def sent_names(conn: sqlite3.Connection, mail_id: int) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM mail_sent_attach WHERE mail_id = ? ORDER BY rowid", (mail_id,))]
