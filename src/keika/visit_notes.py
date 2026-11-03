"""訪問前の1枚に、その画面で書き込んだ答え。

項目が変わっても答えは失わない。記録に送るまでは残し、送った物は次の訪問の欄に出さない（行は消さない）。
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Callable

from keika.settings import Settings, now_real

HOMEWORK = "宿題・持ち越し"
CHECKS = "今確かめること"
GOALS = "目標（様子を聞く）"
ASSESSMENT = "アセスメントで聞けていないこと"
INTENTIONS = "現状に対する意向（予防プラン）"
BASIC = "基本情報"
CHECKLIST = "基本チェックリスト"
LEFTOVER = "項目が変わった入力"


def note_key(section: str, label: str) -> str:
    return hashlib.sha256(f"{section}\n{label}".encode()).hexdigest()[:16]


def save_note(conn: sqlite3.Connection, s: Settings, user_id: str, section: str, label: str, answer: str) -> None:
    conn.execute(
        "INSERT INTO visit_notes (user_id, item_key, section, label, answer, updated_at, used_at) VALUES (?, ?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT (user_id, item_key) DO UPDATE SET answer = excluded.answer, updated_at = excluded.updated_at, used_at = NULL",
        (user_id, note_key(section, label), section, label, answer, now_real().isoformat()),
    )
    conn.commit()


def get_notes(conn: sqlite3.Connection, user_id: str) -> dict[str, dict]:
    """まだ記録に送っていない答え。空にした物は含めない。"""
    rows = conn.execute(
        "SELECT item_key, section, label, answer FROM visit_notes WHERE user_id = ? AND used_at IS NULL AND answer != '' ORDER BY rowid",
        (user_id,),
    ).fetchall()
    return {r["item_key"]: dict(r) for r in rows}


def mark_used(conn: sqlite3.Connection, user_id: str, stamp: str, text: str) -> None:
    """記録に送った文に入っている答えだけを片付ける。stamp より後に書いた答えと、文から消された答えは残す。"""
    for key, n in get_notes(conn, user_id).items():
        if n["answer"] in text:
            conn.execute(
                "UPDATE visit_notes SET used_at = ? WHERE user_id = ? AND item_key = ? AND updated_at <= ?",
                (now_real().isoformat(), user_id, key, stamp),
            )
    conn.commit()


def sheet_items(sheet: Any, q: dict | None, notes: dict[str, dict], fmt: Callable[[str], str]) -> dict[str, list[dict]]:
    """1枚の項目を区分ごとに並べ、書いた答えを添える。今の項目に無い答えは LEFTOVER に集める。"""
    q = q or {}
    groups: dict[str, list[tuple[str, str, Any]]] = {
        HOMEWORK: [(("【確かめる】" if h.flag else "") + h.text, "", None) for h in sheet.homework],
        CHECKS: [(fmt(x["title"]), "", None) for x in sheet.checks],
        GOALS: [(g, "", None) for g in sheet.goals],
        ASSESSMENT: [(f"{g['item']}: {g['question']}", g.get("past", ""), None) for g in q.get("assessment", [])],
        INTENTIONS: [(f"{a['area']} について、本人と家族の意向", a.get("past", ""), None) for a in q.get("intentions", [])],
        BASIC: [(f"{b['field']}: {b['question']}", b.get("past", ""), None) for b in q.get("basic_info", [])],
        CHECKLIST: [(f"{c['no']} {c['question']}", c.get("past", ""), c) for c in q.get("checklist", [])],
    }
    items: dict[str, list[dict]] = {}
    shown = set()
    for section, rows in groups.items():
        items[section] = []
        for label, past, raw in rows:
            key = note_key(section, label)
            if key in shown:  # 同じ文言は1行にまとめる。答えの行き先が1つなので、並べると片方が上書きされる
                continue
            shown.add(key)
            items[section].append({"section": section, "label": label, "past": past, "answer": notes.get(key, {}).get("answer", ""), "raw": raw})
    items[LEFTOVER] = [{"section": n["section"], "label": n["label"], "past": "", "answer": n["answer"], "raw": None} for k, n in notes.items() if k not in shown]
    return items


def to_record_text(name: str, notes: dict[str, dict]) -> str:
    """書いた答えだけを、記録の入力欄に入れる文にする。"""
    lines = [f"{name}様の訪問で聞いたこと。"]
    sections: dict[str, list[str]] = {}
    for n in notes.values():
        sections.setdefault(n["section"], []).append(f"{n['label']} → {n['answer']}")
    for section, rows in sections.items():
        lines += [f"【{section}】", *rows]
    return "\n".join(lines)
