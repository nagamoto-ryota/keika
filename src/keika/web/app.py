"""画面。ログインや権限は作らない（事業所内の PC で動かす）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from keika.db import connect
from keika.findings import Finding
from keika.ledger import User, get_user, list_people, list_users, upsert_user
from keika.machine_checks import HOME_VISIT, PLAN_TYPES, run_machine_checks, user_refs
from keika.reading import stored_facts
from keika.records import list_records
from keika.rules import basis_text, get_rule
from keika.settings import Settings, today

HERE = Path(__file__).parent
KIND_ORDER = {"finding": 0, "unknown": 1, "nudge": 2}
KIND_LABEL = {"finding": "確認してください", "unknown": "分かりません", "nudge": "先回りのお知らせ"}


@dataclass
class Row:
    user: User
    cert_end: str
    plan_end: str
    visit: str
    findings: int
    unknowns: int
    nudges: int


def _dashboard_row(conn, s: Settings, user: User, month: str) -> Row:
    records = list_records(conn, user.id)
    visited = any(r.event_date.startswith(month) and r.kind_read == HOME_VISIT for r in records)
    plans = [f for _, f in stored_facts(conn, user_refs(s, user)) if f.doc_type in PLAN_TYPES and f.period_end]
    found = run_machine_checks(conn, s, user.id)
    cert = user.data.get("cert_period") or ["", ""]
    return Row(
        user=user,
        cert_end=cert[1] or "—",
        plan_end=max((f.period_end for f in plans), default="—"),
        visit="済" if visited else "まだ",
        findings=sum(f.kind == "finding" for f in found),
        unknowns=sum(f.kind == "unknown" for f in found),
        nudges=sum(f.kind == "nudge" for f in found),
    )


def _view(f: Finding) -> dict:
    rule = get_rule(f.rule_id)
    return {"f": f, "label": KIND_LABEL[f.kind], "title": rule.title, "plain": rule.plain, "basis": basis_text(rule)}


def _next_user_id(conn) -> str:
    numbers = [int(u.id[1:]) for u in list_users(conn) if u.id[1:].isdigit()]
    return f"U{max(numbers, default=0) + 1:02d}"


def create_app(s: Settings) -> FastAPI:
    app = FastAPI(title="ケイカ")
    conn = connect(s)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["as_of"] = s.as_of
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/")
    def dashboard(request: Request, cm: str | None = None):
        month = today(s).isoformat()[:7]
        rows = [_dashboard_row(conn, s, u, month) for u in list_users(conn, cm=cm)]
        staff = list_people(conn, "staff")
        return templates.TemplateResponse(request, "dashboard.html", {"rows": rows, "staff": staff, "cm": cm, "month": month})

    @app.get("/users/{user_id}")
    def user_page(request: Request, user_id: str):
        user = get_user(conn, user_id)
        if user is None:
            raise HTTPException(404, "利用者が見つかりません")
        found = sorted(run_machine_checks(conn, s, user_id), key=lambda f: KIND_ORDER[f.kind])
        facts = stored_facts(conn, user_refs(s, user))
        return templates.TemplateResponse(request, "user.html", {
            "user": user,
            "findings": [_view(f) for f in found],
            "docs": [{"name": r.rel_path.rsplit("/", 1)[-1], "facts": f} for r, f in facts],
            "records": list(reversed(list_records(conn, user_id))),
            "unread": len(user_refs(s, user)) - len(facts),
        })

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
