"""訪問前の1枚。前回からの流れ、宿題、今確かめること、期限、目標を1枚に集める。

Claude は呼ばない。保存済みの記録と点検の結果を並べるだけなので、すぐ開けて費用もかからない。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from keika.drafts import Homework, open_homework
from keika.ledger import get_user
from keika.machine_checks import HOME_VISIT, _latest_plan, belongs_to, user_refs
from keika.reading import stored_facts
from keika.records import Record, list_records
from keika.responses import sort_findings
from keika.settings import Settings, today

KIND_ORDER = {"nudge": 0, "finding": 1}


@dataclass
class Sheet:
    user: object
    last_visit: Record | None = None
    since_visit: list[Record] = field(default_factory=list)
    visited_this_month: str | None = None
    homework: list[Homework] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    plan_end: str | None = None
    cert_end: str | None = None
    foreign: list[str] = field(default_factory=list)  # 別の人の名前が読めた書類


def _service(s) -> str:
    if s.per_week is not None:
        return f"{s.name} 週{int(s.per_week) if float(s.per_week).is_integer() else s.per_week}回"
    return f"{s.name} {s.days}" if s.days else s.name


def build_sheet(conn: sqlite3.Connection, s: Settings, user_id: str) -> Sheet | None:
    from keika.web.app import current_findings

    user = get_user(conn, user_id)
    if user is None:
        return None
    sheet = Sheet(user=user, homework=open_homework(conn, user_id))
    records = sorted(list_records(conn, user_id), key=lambda r: (r.event_date, r.event_time, r.id))
    visits = [r for r in records if r.kind_read == HOME_VISIT]
    if visits:
        sheet.last_visit = visits[-1]
        sheet.since_visit = records[records.index(visits[-1]) + 1:]
        month = today(s).isoformat()[:7]
        if visits[-1].event_date.startswith(month):
            sheet.visited_this_month = visits[-1].event_date
    else:
        sheet.since_visit = records[-5:]
    found, _ = current_findings(conn, s, user_id)
    for item in sort_findings(conn, s, user_id, found).open:
        f = item.finding
        if f.kind in KIND_ORDER:
            sheet.checks.append({"title": f.todo or f.message, "message": f.message, "kind": f.kind, "rule": f.rule_id})
    sheet.checks.sort(key=lambda x: KIND_ORDER[x["kind"]])
    facts_all = stored_facts(conn, user_refs(conn, s, user))
    sheet.foreign = [r.rel_path.rsplit("/", 1)[-1] for r, f in facts_all if not belongs_to(f, user)]
    plan = _latest_plan([(r, f) for r, f in facts_all if belongs_to(f, user)])
    if plan:
        facts = plan[1]
        sheet.goals = [g.text for g in facts.goals]
        sheet.services = [_service(x) for x in facts.services]
        sheet.plan_end = facts.period_end
    cert = user.data.get("cert_period") or []
    sheet.cert_end = cert[1] if len(cert) == 2 else None
    return sheet
