"""Gmail から実際に送られたメールを、支援経過の案（入口の確認カード）にする。

確かめるのは、ケイカが下書きに入れたメールのスレッドだけ。読むのは各メールの番号と目印（送信済みか）と日時だけで、
本文は読まない。下書きのまま・消された物は案にしない。記録には自動で入れず、人が〔いいよ〕を押した時に入る。
文は決まった形で作るので AI を呼ばない。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from keika import attach
from keika.gmail_draft import GmailError
from keika.intake import to_review
from keika.settings import Settings, now_real

GIVE_UP_DAYS = 30
PER_CHECK = 5
JST = timezone(timedelta(hours=9))
KIND = "文書・FAX・メール"


def remember_thread(conn: sqlite3.Connection, mail_id: int, thread_id: str) -> None:
    if thread_id:
        conn.execute("INSERT OR REPLACE INTO mail_gmail_thread (mail_id, thread_id) VALUES (?, ?)", (mail_id, thread_id))


def pending(conn: sqlite3.Connection, limit: int = PER_CHECK) -> list[sqlite3.Row]:
    """Gmail に入れて、まだ送ったか分かっていない物。新しい順。"""
    return conn.execute(
        "SELECT o.*, t.thread_id, g.at AS put_at FROM mail_out o JOIN mail_gmail g ON g.mail_id = o.mail_id"
        " JOIN mail_gmail_thread t ON t.mail_id = o.mail_id"
        " WHERE o.state = 'sent' AND o.mail_id NOT IN (SELECT mail_id FROM mail_sent_seen) ORDER BY g.at DESC LIMIT ?", (limit,)).fetchall()


def _settle(conn: sqlite3.Connection, mail_id: int, result: str, review_id: int | None = None) -> None:
    conn.execute("INSERT OR REPLACE INTO mail_sent_seen (mail_id, result, review_id, at) VALUES (?, ?, ?, ?)", (mail_id, result, review_id, now_real().isoformat()))
    conn.commit()


def card_text(row: sqlite3.Row, sent_at: datetime, names: list[str]) -> str:
    purpose = row["purpose"] or "連絡"
    text = f"{sent_at.month}/{sent_at.day} {row['sent_name']}へ、メールで{purpose}した（件名「{row['sent_subject']}」）。"
    return text + (f"添付: {'、'.join(names)}。" if names else "")


def check(conn: sqlite3.Connection, s: Settings, gmail: Any) -> int:
    """送信済みになった物を確認カードにする。作ったカードの数を返す。つないでいなければ何もしない。"""
    if gmail is None:
        return 0
    made = 0
    for row in pending(conn):
        try:
            put_at = datetime.fromisoformat(row["put_at"])
        except ValueError:
            put_at = now_real()
        try:
            state, sent_ms = gmail.sent_state(row["thread_id"])
        except GmailError:
            continue  # つながらない時は、次に開いた時にもう一度
        if state == "gone":
            _settle(conn, row["mail_id"], "gone")
            continue
        if state != "sent":
            if now_real().replace(tzinfo=None) - put_at.replace(tzinfo=None) > timedelta(days=GIVE_UP_DAYS):
                _settle(conn, row["mail_id"], "expired")
            continue
        sent_at = datetime.fromtimestamp(int(sent_ms) / 1000, tz=JST) if sent_ms else now_real().astimezone(JST)
        body = card_text(row, sent_at, attach.sent_names(conn, row["mail_id"]))
        entry = {"user_id": row["user_id"], "event_date": sent_at.date().isoformat(), "event_time": sent_at.strftime("%H:%M"), "kind_read": KIND,
                 "monitoring": False, "partner": row["sent_name"] or "", "body": body, "reason": f"Gmail から送ったメールを、支援経過の案にしました: {body}"}
        review_id = to_review(conn, f"件名: {row['sent_subject']}\n{row['sent_body']}", "mail", entry, [], "")
        _settle(conn, row["mail_id"], "sent", review_id)
        made += 1
    return made
