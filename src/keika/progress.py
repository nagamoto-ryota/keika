"""進捗: この人はどこまで済んだか。今の計画について工程ごとに出す。

日付と有無で分かる工程は保存した読み取り結果から計算し、文章を読む工程は Claude の判定を使う。
読んでいないものは「まだ」と言わず「不明」にする。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from keika.claude_checks import ProgressMark, latest_marks
from keika.ledger import User
from keika.machine_checks import HOME_VISIT, _is_yobo, _latest_plan, _parse, user_refs
from keika.reading import stored_facts
from keika.records import list_records
from keika.settings import Settings, today

ASSESS_BEFORE = timedelta(days=120)  # 計画の開始までこの日数以内のアセスメントを今の計画の分とみなす
ASSESS_AFTER = timedelta(days=31)


@dataclass(frozen=True)
class Step:
    key: str
    label: str


STEPS = (
    Step("assessment", "アセス"),
    Step("meeting", "会議"),
    Step("minutes", "会議録"),
    Step("consent", "同意"),
    Step("delivery", "交付"),
    Step("service_plans", "個別計画"),
    Step("medical", "医療系"),
    Step("visit", "今月の訪問"),
    Step("usage", "利用票"),
)
CLAUDE_STEPS = ("meeting", "delivery", "service_plans", "medical")


@dataclass
class Cell:
    key: str
    label: str
    status: str  # done / check（あるが中身を確かめる）/ todo（無い）/ unknown / na
    date: str = ""
    note: str = ""
    file: str = ""
    quote: str = ""


def user_progress(conn: sqlite3.Connection, s: Settings, user: User) -> list[Cell]:
    on = today(s)
    refs = user_refs(conn, s, user)
    facts = stored_facts(conn, refs)
    plan = _latest_plan(facts)
    marks = latest_marks(conn, user.id)
    done = {
        "assessment": _assessment(facts, plan),
        "minutes": _minutes(facts, plan, len(facts) == len(refs)),
        "consent": _consent(plan),
        "visit": _visit(conn, user, on),
        "usage": _usage(facts, on),
    }
    cells = []
    for step in STEPS:
        if step.key in CLAUDE_STEPS:
            cells.append(_from_mark(step, marks.get(step.key)))
        else:
            cells.append(Cell(step.key, step.label, **done[step.key]))
    _settle_meeting(cells)
    return cells


def _settle_meeting(cells: list[Cell]) -> None:
    """会議録があるなら会議は開かれている。中身の不足は「まだ」でなく「確認」にする。"""
    by_key = {c.key: c for c in cells}
    if by_key["meeting"].status == "todo" and by_key["minutes"].status == "done":
        by_key["meeting"].status = "check"


def _from_mark(step: Step, mark: ProgressMark | None) -> Cell:
    if mark is None:
        return Cell(step.key, step.label, "unknown", note="文章の点検はまだです")
    return Cell(step.key, step.label, mark.status, mark.date, mark.note, mark.file, mark.quote)


def _assessment(facts, plan) -> dict:
    docs = [(r, f) for r, f in facts if f.doc_type == "アセスメント" and _parse(f.doc_date)]
    if plan is None:
        return {"status": "unknown", "note": "計画書を読めていません"}
    start = _parse(plan[1].period_start)
    if start is None:
        return {"status": "unknown", "note": "計画の開始日が分かりません"}
    near = [(r, f) for r, f in docs if start - ASSESS_BEFORE <= _parse(f.doc_date) <= start + ASSESS_AFTER]
    if near:
        ref, f = max(near, key=lambda d: d[1].doc_date)
        return {"status": "done", "date": f.doc_date, "file": ref.rel_path}
    return {"status": "todo", "note": "今の計画に対応するアセスメントが見当たりません"}


def _minutes(facts, plan, all_read: bool) -> dict:
    if plan is None:
        return {"status": "unknown", "note": "計画書を読めていません"}
    docs = [(r, f) for r, f in facts if f.doc_type == "サービス担当者会議の要点" and f.doc_date]
    if docs:
        ref, f = max(docs, key=lambda d: d[1].doc_date)
        if f.doc_date >= plan[1].period_start or (_parse(f.doc_date) and _parse(plan[1].period_start) and
                                                  _parse(plan[1].period_start) - _parse(f.doc_date) <= ASSESS_BEFORE):
            return {"status": "done", "date": f.doc_date, "file": ref.rel_path}
    if not all_read:
        return {"status": "unknown", "note": "未読の書類があります"}
    return {"status": "todo", "note": "今の計画の会議の要点が見当たりません"}


def _consent(plan) -> dict:
    if plan is None:
        return {"status": "unknown", "note": "計画書を読めていません"}
    ref, f = plan
    if not f.consent_date:
        return {"status": "unknown", "note": "同意日を読み取れませんでした", "file": ref.rel_path}
    if f.consent_date > f.period_start:
        return {"status": "check", "date": f.consent_date, "note": "計画の開始より後の同意日です", "file": ref.rel_path}
    return {"status": "done", "date": f.consent_date, "file": ref.rel_path}


def _visit(conn, user: User, on: date) -> dict:
    month = on.isoformat()[:7]
    rows = [r for r in list_records(conn, user.id) if r.event_date.startswith(month) and r.kind_read == HOME_VISIT]
    if rows:
        return {"status": "done", "date": rows[-1].event_date}
    if _is_yobo(user):
        return {"status": "na", "note": "予防は3か月に1回です。お知らせで確かめます"}
    return {"status": "todo"}


def _usage(facts, on: date) -> dict:
    month = on.isoformat()[:7]
    docs = [(r, f) for r, f in facts if f.doc_type == "利用票" and f.target_month == month]
    if not docs:
        return {"status": "unknown", "note": "今月の利用票を読めていません"}
    ref, f = docs[-1]
    status = {True: "done", False: "todo", None: "unknown"}[f.signature_present]
    return {"status": status, "date": f.signature_date or "", "file": ref.rel_path}
