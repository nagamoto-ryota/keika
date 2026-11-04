"""画面。ログインや権限は作らない（事業所内の PC で動かす）。"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, get_args
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from urllib.parse import urlparse

from keika.claude import make_client
from keika.care_plan import EditConflict, add_line, add_row, check_against_assessment, edit_cell, edit_history, get_care_plan, list_care_plans, make_care_plan, remove_row, save_to_output, to_docx as care_plan_docx
from keika.claude_checks import APP_RECORDS, CheckRun, check_and_save, latest_run
from keika.db import connect as _connect
from keika.next_steps import get_plan, make_plan
from keika.visit_questions import get_questions, make_questions, questions_key
from keika.visit_sheet import build_sheet
from keika.drafts import SECTIONS, draft_for_results, finish_homework, move_record_drafts, to_docx, get_draft, list_drafts, mark_checked as mark_draft_checked, plain_text, sync_review, update_draft
from keika.db import ThreadConnection, connect
from keika.files import ALL_EXTS, BINARY_EXTS
from keika.guide import consent_docx
from keika.findings import Finding, fingerprint
from keika.claude_checks import crosses_line
from keika.legal_texts import basis_for
from keika.med_names import decide_fix, decide_keep, decisions, message as med_message, open_checks, unconfirmed_record_ids
from keika.visit_notes import get_notes, mark_used, save_note, sheet_items, to_record_text
from keika.intake import IntakeResult, open_reviews, original_of, process_text, resolve_review, review_original, to_review
from keika.text_compare import compare
from keika.ledger import User, get_user, list_people, list_users, upsert_user
from keika.machine_checks import HOME_VISIT, PLAN_TYPES, run_machine_checks, user_refs
from keika.office import declare, office_rows
from keika.progress import STEPS, user_progress
from keika.reading import stored_facts
from keika.records import KindRead, RecordIn, add_record, edit_record, get_record, history, list_records, mark_checked
from keika.responses import Item, respond, silenced_counts, silenced_list, sort_findings
from keika.rules import basis_text, get_rule
from keika.tasks import build_board
from keika.settings import OutsideRootError, Settings, now_real, safe_path, today
from keika.views import evidence_view, hdate

HERE = Path(__file__).parent
KIND_ORDER = {"finding": 0, "unknown": 1, "nudge": 2}
KIND_LABEL = {"finding": "確認", "unknown": "読み取れず", "nudge": "早めのお知らせ"}


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


OFFICE_TAB = "office"


def _dashboard_row(conn, s: Settings, user: User, month: str) -> Row:
    records = list_records(conn, user.id)
    visited = any(r.event_date.startswith(month) and r.kind_read == HOME_VISIT for r in records)
    plans = [f for _, f in stored_facts(conn, user_refs(conn, s, user)) if f.doc_type in PLAN_TYPES and f.period_end]
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
        "label": KIND_LABEL[f.kind], "title": rule.title, "plain": rule.plain, "basis": basis_text(rule), "added_by": rule.added_by,
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


def _host_allowed(host: str) -> bool:
    """このPC、同じ事業所内のネットワークのアドレス、このPCの名前だけを受ける。"""
    name = host.rsplit(":", 1)[0].strip("[]").lower()
    if name in ("localhost", "127.0.0.1", "::1", "testserver", socket.gethostname().lower()):
        return True
    extra = os.environ.get("KEIKA_ALLOWED_HOSTS", "")
    if name in {h.strip().lower() for h in extra.split(",") if h.strip()}:
        return True
    try:
        return ipaddress.ip_address(name).is_private
    except ValueError:
        return False


def _next_user_id(conn) -> str:
    numbers = [int(u.id[1:]) for u in list_users(conn) if u.id[1:].isdigit()]
    return f"U{max(numbers, default=0) + 1:02d}"


def create_app(s: Settings, client_factory: Callable[[], Any] = make_client, sample: bool = False) -> FastAPI:
    app = FastAPI(title="ケイカ")
    conn = ThreadConnection(s)
    connect(s).close()  # 表を先に作る
    if sample:
        @app.middleware("http")
        async def refuse_writes(request: Request, call_next):
            if request.method == "POST":  # 見本は同梱の結果を見るだけ。直せる経路を残さない
                return PlainTextResponse("見本モードでは変更できません。API キーを設定して keika serve で起動してください。", status_code=403)
            return await call_next(request)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["as_of"] = s.as_of
    templates.env.globals["sample"] = sample
    templates.env.globals["legal"] = basis_for
    templates.env.globals["med_message"] = med_message
    templates.env.globals["crosses_line"] = crosses_line
    templates.env.filters["blanks"] = _blanks
    templates.env.filters["mark_quote"] = _mark_quote
    templates.env.filters["hdate_full"] = lambda text: (text or "").replace("-", "/")
    templates.env.filters["hdate"] = lambda text: hdate(text, today(s).year)

    @app.middleware("http")
    async def same_place_only(request: Request, call_next):
        """外のページからの送信と、DNS を使った読み出しを断る。ログインが無いので、ここが入口の守り。"""
        host = request.headers.get("host", "")
        if not _host_allowed(host):
            return PlainTextResponse("このホスト名では開けません", status_code=400)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlparse(origin).netloc != host:
            return PlainTextResponse("外のページからは送信できません", status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # 直した JS が古いまま動かないように、毎回確かめさせる
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def _reviews_for(cm: str | None) -> list[dict]:
        """担当の絞り込み中は、その人の分と、誰の話か決まっていない分を出す。"""
        if cm == OFFICE_TAB:
            return []
        mine = {u.id for u in list_users(conn, cm=cm)} if cm else None
        return [it for it in open_reviews(conn) if mine is None or not it["entry"].get("user_id") or it["entry"]["user_id"] in mine]

    def _office_for(cm: str | None) -> list:
        if cm and cm != OFFICE_TAB:
            return []
        rows = [r for r in office_rows(conn, s) if r.status in ("todo", "unknown")]
        for row in rows:
            row.basis = basis_text(row.rule)
        return rows

    @app.get("/")
    def dashboard(request: Request, cm: str | None = None):
        month = today(s).isoformat()[:7]
        board = build_board(conn, s, cm=None if cm == OFFICE_TAB else cm)
        if cm == OFFICE_TAB:
            board.people, board.visit_waiting, board.unknowns = [], [], []
        year = today(s).year
        for person in board.people:
            records = list_records(conn, person.user.id)
            for task in person.tasks:
                task.view = _view(s, task.item, records, year) if task.item else None
        return templates.TemplateResponse(request, "dashboard.html", {
            "board": board, "staff": list_people(conn, "staff"), "cm": cm, "users": list_users(conn),
            "reviews": _reviews_for(cm), "office": _office_for(cm),
            "med_checks": [] if cm == OFFICE_TAB else open_checks(conn, s, cm=cm),
            "drafts": _drafts_for(cm), "user_names": {u.id: u.name for u in list_users(conn)},
        })

    def _drafts_for(cm: str | None) -> list:
        if cm == OFFICE_TAB:
            return []
        rows = list_drafts(conn)
        if not cm:
            return rows
        mine = {u.id for u in list_users(conn) if u.cm == cm}
        return [d for d in rows if d.user_id in mine]

    def _draft_in_background(client, results, source: str) -> None:
        own = _connect(s)
        try:
            draft_for_results(own, client, s, results, source)
        finally:
            own.close()

    def _draft_or_404(draft_id: int):
        draft = get_draft(conn, draft_id)
        if draft is None:
            raise HTTPException(404, "下書きが見つかりません")
        return draft

    @app.get("/drafts/{draft_id}")
    def draft_page(request: Request, draft_id: int):
        draft = _draft_or_404(draft_id)
        user = get_user(conn, draft.user_id)
        staff = {p.id: p.name for p in list_people(conn, "staff")}
        return templates.TemplateResponse(request, "draft.html", {
            "d": draft, "user": user, "author": staff.get(user.cm, ""), "plain": plain_text(draft, user.name),
        })

    @app.post("/drafts/{draft_id}")
    async def save_draft(request: Request, draft_id: int):
        if sample:
            raise HTTPException(403, "見本モードでは直せません")
        draft = _draft_or_404(draft_id)
        form = await request.form()
        people = zip(form.getlist("attendee_org"), form.getlist("attendee_role"), form.getlist("attendee_name"))
        attendees = [{"org": o.strip(), "role": r.strip(), "name": n.strip()} for o, r, n in people if o.strip() or r.strip() or n.strip()]
        sections = {}
        for key in SECTIONS[draft.kind]:
            if f"s_{key}" in form:
                texts, ids = form.getlist(f"s_{key}"), form.getlist(f"s_{key}_id")
                sections[key] = [{"id": ids[i] if i < len(ids) else "", "text": text} for i, text in enumerate(texts)]
        update_draft(conn, draft_id, place=str(form.get("place", "")).strip(), start_time=str(form.get("start_time", "")).strip(),
                     end_time=str(form.get("end_time", "")).strip(), attendees=attendees, sections=sections)
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    @app.get("/drafts/{draft_id}/docx")
    def draft_docx(draft_id: int):
        draft = _draft_or_404(draft_id)
        user = get_user(conn, draft.user_id)
        staff = {p.id: p.name for p in list_people(conn, "staff")}
        name = quote(f"{draft.title}_{draft.event_date}.docx")
        return Response(
            to_docx(draft, user.name, staff.get(user.cm, "")),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
        )

    @app.post("/drafts/{draft_id}/items/{item_id}/done")
    def done_homework(draft_id: int, item_id: str):
        _draft_or_404(draft_id)
        try:
            finish_homework(conn, draft_id, item_id)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        return RedirectResponse("/", status_code=303)

    def _sheet_view(sheet, failed: bool) -> dict:
        q = get_questions(conn, sheet.user.id)
        items = sheet_items(sheet, q, get_notes(conn, sheet.user.id), lambda text: hdate(text, today(s).year))
        return {"sheet": sheet, "q": q, "items": items, "failed": failed, "stale": bool(q) and q.get("key") != questions_key(conn, s, sheet.user)}

    @app.get("/users/{user_id}/visit")
    def visit_page(request: Request, user_id: str):
        sheet = build_sheet(conn, s, user_id)
        if sheet is None:
            raise HTTPException(404, "利用者が見つかりません")
        # 戻るボタンで古い答えを開き直すと、送った答えが生き返るので、保存させない
        return templates.TemplateResponse(request, "visit_sheet.html", _sheet_view(sheet, False), headers={"Cache-Control": "no-store"})

    @app.post("/users/{user_id}/visit/notes")
    def post_note(user_id: str, section: str = Form(...), label: str = Form(...), answer: str = Form("")):
        if get_user(conn, user_id) is None:
            raise HTTPException(404, "利用者が見つかりません")
        save_note(conn, s, user_id, section, label, answer)
        return {"ok": True}

    @app.post("/users/{user_id}/visit/to-record")
    def visit_to_record(request: Request, user_id: str):
        user = get_user(conn, user_id)
        if user is None:
            raise HTTPException(404, "利用者が見つかりません")
        stamp = now_real().isoformat()  # 読む前に取る。読んだ後の保存を「送った」扱いにしないため
        notes = get_notes(conn, user_id)
        if not notes:
            return RedirectResponse(f"/users/{user_id}/visit", status_code=303)
        return templates.TemplateResponse(request, "new_record.html", {
            "text": to_record_text(user.name, notes), "visit_user": user_id, "visit_stamp": stamp,
        })

    @app.post("/users/{user_id}/visit/questions")
    def post_questions(request: Request, user_id: str):
        sheet = build_sheet(conn, s, user_id)
        if sheet is None:
            raise HTTPException(404, "利用者が見つかりません")
        try:
            made = make_questions(conn, client_factory(), s, user_id)
        except RuntimeError:  # キーが無い時
            made = None
        if made is None:
            return templates.TemplateResponse(request, "visit_sheet.html", _sheet_view(sheet, True))
        return RedirectResponse(f"/users/{user_id}/visit", status_code=303)

    @app.post("/drafts/{draft_id}/checked")
    def check_draft(draft_id: int):
        if sample:
            raise HTTPException(403, "見本モードでは直せません")
        draft = _draft_or_404(draft_id)
        mark_draft_checked(conn, draft_id)
        return RedirectResponse(f"/users/{draft.user_id}", status_code=303)

    def _check_in_background(user_id: str) -> None:
        """記録が保存されたら、その利用者だけ点検し直す。失敗しても記録は失わず「未実施」と出す。"""
        check_and_save(s, client_factory, user_id)

    @app.get("/records/new")
    def new_record(request: Request):
        return templates.TemplateResponse(request, "new_record.html", {})

    @app.post("/records/new")
    def post_record(request: Request, background: BackgroundTasks, text: str = Form(...), mode: str = Form("shape"), visit_user: str = Form(""), visit_stamp: str = Form("")):
        if sample:
            raise HTTPException(403, "見本モードでは記録できません。API キーを設定して keika serve で起動してください。")
        if not text.strip():
            raise HTTPException(400, "文章を入れてください")
        if visit_user and visit_stamp and not get_notes(conn, visit_user):  # 同じ送信の二度目
            return RedirectResponse(f"/users/{visit_user}/visit", status_code=303)
        try:
            client = client_factory()
        except RuntimeError as e:  # キーが無くても、話した内容は失わない
            results = [IntakeResult(review_id=to_review(conn, text.strip(), "画面", {}, [], f"整えられませんでした（{e}）"))]
        else:
            saved: list[str] = []
            results = process_text(conn, client, s, text.strip(), "画面", on_saved=saved.append)
            background.add_task(_draft_in_background, client, results, "画面")  # 点検は長いので、下書きを先に作る
            for uid in saved:
                background.add_task(_check_in_background, uid)
        if visit_user and visit_stamp and any(r.review_id or (r.record and r.record.user_id == visit_user) for r in results):
            mark_used(conn, visit_user, visit_stamp, text)  # この人の記録か確認に入った分は、次の訪問の欄に出さない
        names = {u.id: u.name for u in list_users(conn)}
        return templates.TemplateResponse(request, "new_result.html", {"results": results, "names": names})

    def _record_or_404(record_id: int):
        record = get_record(conn, record_id)
        if record is None:
            raise HTTPException(404, "記録が見つかりません")
        return record

    @app.get("/records/{record_id}/edit")
    def edit_form(request: Request, record_id: int):
        return templates.TemplateResponse(request, "edit_record.html", {"r": _record_or_404(record_id), "users": list_users(conn), "kinds": get_args(KindRead)})

    @app.post("/records/{record_id}/edit")
    def edit_save(
        record_id: int, background: BackgroundTasks, user_id: str = Form(...), event_date: str = Form(...), event_time: str = Form(""),
        kind_written: str = Form(""), kind_read: str = Form(""), partner: str = Form(""), body: str = Form(...), author: str = Form(""),
    ):
        before = _record_or_404(record_id)
        if not body.strip():
            raise HTTPException(400, "本文を入れてください")
        if get_user(conn, user_id) is None:
            raise HTTPException(400, "台帳に無い利用者です")
        changes = dict(user_id=user_id, event_date=event_date, event_time=event_time, kind_written=kind_written, kind_read=kind_read or None, partner=partner, body=body.strip(), author=author)
        try:
            edit_record(conn, record_id, changes, editor=author or "画面")
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if before.user_id != user_id:
            move_record_drafts(conn, record_id, user_id)
        for uid in {before.user_id, user_id}:
            background.add_task(_check_in_background, uid)
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.get("/records/{record_id}/history")
    def record_history(request: Request, record_id: int):
        names = {u.id: u.name for u in list_users(conn)}
        return templates.TemplateResponse(request, "history.html", {"r": _record_or_404(record_id), "versions": history(conn, record_id), "names": names, "original": original_of(conn, record_id), "med_decisions": decisions(conn, record_id)})

    @app.post("/med/{check_id}/fix")
    def med_fix(check_id: int, chosen: str = Form(""), editor: str = Form("")):
        try:
            decide_fix(conn, s, check_id, chosen, editor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return RedirectResponse("/#kusuri", status_code=303)

    @app.post("/med/{check_id}/keep")
    def med_keep(check_id: int, editor: str = Form("")):
        try:
            decide_keep(conn, check_id, editor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return RedirectResponse("/#kusuri", status_code=303)

    @app.post("/records/{record_id}/checked")
    def record_checked(record_id: int):
        record = _record_or_404(record_id)
        mark_checked(conn, record_id, editor=record.author or "画面")
        return RedirectResponse(f"/users/{record.user_id}", status_code=303)

    @app.get("/office")
    def office_page(request: Request):
        rows = office_rows(conn, s)
        for row in rows:
            row.basis = basis_text(row.rule)
        return templates.TemplateResponse(request, "office.html", {"rows": rows, "todo": sum(r.status == "todo" for r in rows)})

    @app.post("/office/{rule_id}/declare")
    def office_declare(rule_id: str, who: str = Form(""), basis: str = Form("")):
        try:
            declare(conn, rule_id, who, basis)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return RedirectResponse("/office", status_code=303)

    @app.get("/review")
    def review(request: Request):
        return RedirectResponse("/#kakunin", status_code=303)

    @app.post("/review/{review_id}/compare")
    def post_review_compare(review_id: int, body: str = Form("")):
        """整えた文を直している間の印の付け直し。計算するだけで、何も保存しない。"""
        original = review_original(conn, review_id)
        if original is None:
            raise HTTPException(404, "要確認の項目が見つかりません")
        c = compare(original, body)
        return {"mode": c.mode, "left": c.left, "right": c.right, "legend": c.legend}

    @app.post("/review/{review_id}")
    def post_review(review_id: int, background: BackgroundTasks, user_id: str = Form(""), body: str = Form(""), action: str = Form("save")):
        chosen = user_id if action == "save" and user_id else None
        try:
            record = resolve_review(conn, review_id, user_id=chosen, body=body or None, s=s)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        sync_review(conn, review_id, chosen, record.id if record else None)
        if record:
            background.add_task(_check_in_background, record.user_id)
        return RedirectResponse("/#kakunin", status_code=303)

    @app.post("/users/{user_id}/plan")
    def post_plan(request: Request, user_id: str, fp: str = Form(...)):
        if sample:
            raise HTTPException(403, "見本モードでは作れません")
        finding = _find(user_id, fp)
        try:
            made = make_plan(conn, client_factory(), s, finding)
        except RuntimeError:  # キーが無い時
            made = None
        if made is None:
            return templates.TemplateResponse(request, "plan.html", {"user": get_user(conn, user_id), "f": finding, "plan": None, "failed": True})
        return RedirectResponse(f"/users/{user_id}/plan/{fp}", status_code=303)

    @app.get("/users/{user_id}/plan/{fp}")
    def plan_page(request: Request, user_id: str, fp: str):
        hit = _find(user_id, fp)
        return templates.TemplateResponse(request, "plan.html", {"user": get_user(conn, user_id), "f": hit, "plan": get_plan(conn, hit), "failed": False})

    @app.get("/board")
    def board_page(request: Request, cm: str | None = None):
        month = today(s).isoformat()[:7]
        rows = [_dashboard_row(conn, s, u, month) for u in list_users(conn, cm=cm)]
        staff = list_people(conn, "staff")
        summary = _summary(conn, staff, rows) if not cm else []
        return templates.TemplateResponse(request, "board.html", {"rows": rows, "staff": staff, "cm": cm, "summary": summary, "steps": STEPS, "med_waiting_users": {m.record.user_id for m in open_checks(conn, s)}})

    @app.get("/users/{user_id}")
    def user_page(request: Request, user_id: str):
        user = get_user(conn, user_id)
        if user is None:
            raise HTTPException(404, "利用者が見つかりません")
        found, run = current_findings(conn, s, user_id)
        parts = sort_findings(conn, s, user_id, found)
        records = list_records(conn, user_id)
        year = today(s).year
        facts = stored_facts(conn, user_refs(conn, s, user))
        return templates.TemplateResponse(request, "user.html", {
            "med_waiting": unconfirmed_record_ids(conn, s),
            "user": user,
            "findings": [_view(s, i, records, year) for i in parts.open],
            "noted": [_view(s, i, records, year) for i in parts.noted],
            "run": run,
            "docs": [{"name": r.rel_path.rsplit("/", 1)[-1], "facts": f} for r, f in facts],
            "records": list(reversed(records)),
            "unread": len(user_refs(conn, s, user)) - len(facts),
            "cells": user_progress(conn, s, user),
            "drafts": list_drafts(conn, user_id, status=None),
            "care_plans": list_care_plans(conn, user_id),
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
        ext = target.suffix.lower().lstrip(".")
        if ext not in ALL_EXTS:  # HTML や SVG を、アプリと同じ場所で実行させない
            raise HTTPException(403, "この種類のファイルは開けません")
        inline = ext in BINARY_EXTS
        return FileResponse(target, content_disposition_type="inline" if inline else "attachment", filename=target.name,
                            headers={"X-Content-Type-Options": "nosniff"})

    def _care_plan_or_404(plan_id: int):
        try:
            plan = get_care_plan(conn, plan_id)
        except OverflowError:  # データベースの整数に入らない番号
            plan = None
        if plan is None:
            raise HTTPException(404, "下書きが見つかりません")
        return plan

    def _care_plan_page(request: Request, plan, compare: str = "", saved: str = ""):
        return templates.TemplateResponse(request, "care_plan.html", {
            "p": plan, "user": get_user(conn, plan.user_id), "compare": compare, "saved": saved, "records_name": APP_RECORDS, "history": edit_history(conn, plan.id),
        }, headers={"Cache-Control": "no-store"})  # 戻るボタンで古い文を開き直して、新しい手直しを上書きしない

    @app.post("/users/{user_id}/careplan")
    def post_care_plan(request: Request, user_id: str):
        user = get_user(conn, user_id)
        if user is None:
            raise HTTPException(404, "利用者が見つかりません")
        try:
            made = make_care_plan(conn, client_factory(), s, user_id)
        except RuntimeError:  # キーが無い時
            made = None
        if made is None:
            return PlainTextResponse("ケアプランの下書きを作れませんでした。時間をおいて、もう一度押してください。", status_code=502)
        return RedirectResponse(f"/careplans/{made}", status_code=303)

    @app.get("/careplans/{plan_id}")
    def care_plan_page(request: Request, plan_id: int, compare: str = "", saved: str = ""):
        return _care_plan_page(request, _care_plan_or_404(plan_id), compare, saved)

    @app.post("/careplans/{plan_id}/cells")
    def save_care_plan_cell(plan_id: int, item: str = Form(...), text: str = Form(""), before: str = Form(""), before_sent: str = Form("")):
        _care_plan_or_404(plan_id)
        try:  # 空の文は Form で None と区別できないので、送ったことを別に受ける
            edit_cell(conn, plan_id, item, text, before if before_sent else None)
        except KeyError:
            raise HTTPException(404, "そのマスが見つかりません") from None
        except EditConflict:
            raise HTTPException(409, "別の画面で直されています") from None
        return {"ok": True}

    @app.post("/careplans/{plan_id}/lines")
    def add_care_plan_line(plan_id: int, key: str = Form(...)):
        plan = _care_plan_or_404(plan_id)
        if key not in plan.sections:
            raise HTTPException(400, "その欄はありません")
        add_line(conn, plan_id, key)
        return RedirectResponse(f"/careplans/{plan_id}", status_code=303)

    @app.post("/careplans/{plan_id}/rows")
    def add_care_plan_row(plan_id: int):
        _care_plan_or_404(plan_id)
        add_row(conn, plan_id)
        return RedirectResponse(f"/careplans/{plan_id}", status_code=303)

    @app.post("/careplans/{plan_id}/rows/remove")
    def remove_care_plan_row(plan_id: int, item: str = Form(...)):
        _care_plan_or_404(plan_id)
        try:
            remove_row(conn, plan_id, item)
        except KeyError:
            raise HTTPException(404, "その行が見つかりません") from None
        return RedirectResponse(f"/careplans/{plan_id}", status_code=303)

    @app.post("/careplans/{plan_id}/export")
    def export_care_plan(plan_id: int):
        plan = _care_plan_or_404(plan_id)
        try:
            rel = save_to_output(s, plan)
        except OSError:
            return RedirectResponse(f"/careplans/{plan_id}?saved=failed", status_code=303)
        return RedirectResponse(f"/careplans/{plan_id}?saved={quote(rel)}", status_code=303)

    @app.post("/careplans/{plan_id}/compare")
    def compare_care_plan(plan_id: int):
        _care_plan_or_404(plan_id)
        try:
            done = check_against_assessment(conn, client_factory(), s, plan_id)
        except RuntimeError:  # キーが無い時
            done = None
        suffix = {True: "", False: "?compare=none", None: "?compare=failed"}[done]
        return RedirectResponse(f"/careplans/{plan_id}{suffix}", status_code=303)

    @app.get("/careplans/{plan_id}/docx")
    def care_plan_word(plan_id: int):
        plan = _care_plan_or_404(plan_id)
        user = get_user(conn, plan.user_id)
        name = quote(f"{plan.title}_下書き_{user.name}.docx")
        return Response(
            care_plan_docx(plan),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
        )

    @app.get("/guide")
    def guide_page(request: Request):
        return templates.TemplateResponse(request, "guide.html", {})

    @app.get("/guide/consent.docx")
    def guide_consent():
        name = quote("AI利用の同意書_たたき台.docx")
        return Response(
            consent_docx(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
        )

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
