"""画面。ログインや権限は作らない（事業所内の PC で動かす）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from keika.claude_checks import CheckRun, latest_run
from keika.db import connect
from keika.findings import Finding, fingerprint
from keika.ledger import User, get_user, list_people, list_users, upsert_user
from keika.machine_checks import HOME_VISIT, PLAN_TYPES, run_machine_checks, user_refs
from keika.progress import STEPS, user_progress
from keika.reading import stored_facts
from keika.records import RecordIn, add_record, list_records
from keika.responses import Item, respond, silenced_counts, silenced_list, sort_findings
from keika.rules import basis_text, get_rule
from keika.tasks import build_board
from keika.settings import OutsideRootError, Settings, safe_path, today
from keika.views import evidence_view, hdate

HERE = Path(__file__).parent
KIND_ORDER = {"finding": 0, "unknown": 1, "nudge": 2}
KIND_LABEL = {"finding": "確認", "unknown": "分からない", "nudge": "先回り"}


@dataclass
class Row:
    user: User
    cert_end: str
    plan_end: str
    visit: str
    cells: list
    findings: int
    unknowns: int
    nudges: int
    closed: int = 0


def current_findings(conn, s: Settings, user_id: str) -> tuple[list[Finding], CheckRun | None]:
    """機械の点検と最新の Claude の点検を合わせる。Claude が事情を読んで取り下げた候補は外す。"""
    latest = latest_run(conn, user_id)
    run = latest[0] if latest else None
    dismissed = {(d.rule_id, d.period) for d in run.dismissed} if run else set()
    machine = [f for f in run_machine_checks(conn, s, user_id) if (f.rule_id, f.period) not in dismissed]
    return sorted(machine + (run.findings if run else []), key=lambda f: KIND_ORDER[f.kind]), run


def _blanks(text: str | None) -> Markup:
    """書き足し案の「〇〇」の空欄を強調する。"""
    return Markup(re.sub("〇+", lambda m: f'<mark class="blank">{m[0]}</mark>', str(escape(text or ""))))


def _mark_quote(text: str | None, quote: str | None) -> Markup:
    """原文の中の引用の箇所を強調する。"""
    body = str(escape(text or ""))
    needle = str(escape(quote or ""))
    return Markup(body.replace(needle, f"<mark>{needle}</mark>", 1) if needle else body)


def _dashboard_row(conn, s: Settings, user: User, month: str) -> Row:
    records = list_records(conn, user.id)
    visited = any(r.event_date.startswith(month) and r.kind_read == HOME_VISIT for r in records)
    plans = [f for _, f in stored_facts(conn, user_refs(s, user)) if f.doc_type in PLAN_TYPES and f.period_end]
    found, _ = current_findings(conn, s, user.id)
    found = [i.finding for i in sort_findings(conn, s, user.id, found).open]
    latest = latest_run(conn, user.id)
    cert = user.data.get("cert_period") or ["", ""]
    return Row(
        user=user,
        cert_end=cert[1] or "—",
        plan_end=max((f.period_end for f in plans), default="—"),
        visit="済" if visited else "まだ",
        cells=user_progress(conn, s, user),
        findings=sum(f.kind == "finding" for f in found),
        unknowns=sum(f.kind == "unknown" for f in found),
        nudges=sum(f.kind == "nudge" for f in found),
        closed=len(latest[0].resolved) if latest else 0,
    )


def _view(s: Settings, item: Item, records: list, year: int) -> dict:
    f = item.finding
    rule = get_rule(f.rule_id)
    return {
        "f": f, "fp": item.fingerprint, "changed": item.evidence_changed, "record_id": item.record_id,
        "label": KIND_LABEL[f.kind], "title": rule.title, "plain": rule.plain, "basis": basis_text(rule),
        "evidence": [evidence_view(s, e, records, year) for e in f.evidence],
    }


def _summary(conn, staff: list, rows: list[Row]) -> list[dict]:
    """管理者が一目で見る、担当者ごとの集計。"""
    silenced = silenced_counts(conn)
    out = []
    for person in staff:
        mine = [r for r in rows if r.user.cm == person.id]
        if mine:
            out.append({
                "name": person.name, "open": sum(r.findings for r in mine),
                "silenced": sum(silenced.get(r.user.id, 0) for r in mine), "closed": sum(r.closed for r in mine),
            })
    return out


def _next_user_id(conn) -> str:
    numbers = [int(u.id[1:]) for u in list_users(conn) if u.id[1:].isdigit()]
    return f"U{max(numbers, default=0) + 1:02d}"


def create_app(s: Settings) -> FastAPI:
    app = FastAPI(title="ケイカ")
    conn = connect(s)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["as_of"] = s.as_of
    templates.env.filters["blanks"] = _blanks
    templates.env.filters["mark_quote"] = _mark_quote
    templates.env.filters["hdate_full"] = lambda text: (text or "").replace("-", "/")
    templates.env.filters["hdate"] = lambda text: hdate(text, today(s).year)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/")
    def dashboard(request: Request, cm: str | None = None):
        month = today(s).isoformat()[:7]
        board = build_board(conn, s, cm=cm)
        year = today(s).year
        for person in board.people:
            records = list_records(conn, person.user.id)
            for task in person.tasks:
                task.view = _view(s, task.item, records, year) if task.item else None
        return templates.TemplateResponse(request, "dashboard.html", {"board": board, "staff": list_people(conn, "staff"), "cm": cm})

    @app.get("/board")
    def board_page(request: Request, cm: str | None = None):
        month = today(s).isoformat()[:7]
        rows = [_dashboard_row(conn, s, u, month) for u in list_users(conn, cm=cm)]
        staff = list_people(conn, "staff")
        summary = _summary(conn, staff, rows) if not cm else []
        return templates.TemplateResponse(request, "board.html", {"rows": rows, "staff": staff, "cm": cm, "summary": summary, "steps": STEPS})

    @app.get("/users/{user_id}")
    def user_page(request: Request, user_id: str):
        user = get_user(conn, user_id)
        if user is None:
            raise HTTPException(404, "利用者が見つかりません")
        found, run = current_findings(conn, s, user_id)
        parts = sort_findings(conn, s, user_id, found)
        records = list_records(conn, user_id)
        year = today(s).year
        facts = stored_facts(conn, user_refs(s, user))
        return templates.TemplateResponse(request, "user.html", {
            "user": user,
            "findings": [_view(s, i, records, year) for i in parts.open],
            "noted": [_view(s, i, records, year) for i in parts.noted],
            "run": run,
            "docs": [{"name": r.rel_path.rsplit("/", 1)[-1], "facts": f} for r, f in facts],
            "records": list(reversed(records)),
            "unread": len(user_refs(s, user)) - len(facts),
            "cells": user_progress(conn, s, user),
        })

    def _find(user_id: str, fp: str) -> Finding:
        found, _ = current_findings(conn, s, user_id)
        hit = next((f for f in found if fingerprint(f) == fp), None)
        if hit is None:
            raise HTTPException(404, "その指摘は見つかりません")
        return hit

    @app.post("/users/{user_id}/respond")
    def respond_finding(user_id: str, fp: str = Form(...), kind: str = Form(...), note: str = Form(""), back: str = Form("")):
        target = _find(user_id, fp)
        record_id = None
        if kind == "history_noted":
            if not note.strip():
                raise HTTPException(400, "経緯の文を書いてください")
            # 過去の漏れは、今日の日付で経緯を残す。過去の日付では書き足さない
            record_id = add_record(conn, RecordIn(user_id=user_id, event_date=today(s).isoformat(), kind_written="経緯", body=note.strip())).id
        respond(conn, s, target, kind, record_id=record_id)
        safe_back = back if back.startswith("/") and not back.startswith("//") else f"/users/{user_id}"
        return RedirectResponse(safe_back, status_code=303)

    @app.get("/doc")
    def open_doc(path: str):
        """根拠の書類を開く。読み取りの根の外は開けない。"""
        try:
            target = safe_path(s, path)
        except OutsideRootError:
            raise HTTPException(403, "このフォルダの外は開けません") from None
        if not target.is_file():
            raise HTTPException(404, "書類が見つかりません")
        return FileResponse(target, content_disposition_type="inline", filename=target.name)

    @app.get("/silenced")
    def silenced(request: Request):
        return templates.TemplateResponse(request, "silenced.html", {"rows": silenced_list(conn)})

    @app.get("/ledger")
    def ledger(request: Request):
        return templates.TemplateResponse(request, "ledger.html", {"users": list_users(conn), "staff": list_people(conn, "staff")})

    @app.post("/ledger")
    def ledger_save(
        id: str = Form(""), name: str = Form(...), kana: str = Form(""), cm: str = Form(""), care_level: str = Form(""),
        cert_start: str = Form(""), cert_end: str = Form(""), type: str = Form(""),
    ):
        current = get_user(conn, id) if id else None
        data = dict(current.data) if current else {}
        data.update(care_level=care_level, type=type, cert_period=[cert_start, cert_end])
        upsert_user(conn, User(id=id or _next_user_id(conn), name=name, kana=kana, cm=cm, data=data))
        return RedirectResponse("/ledger", status_code=303)

    return app
