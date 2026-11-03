"""機械の点検。Claude が読み取って保存した項目に対する、日付と有無の計算。文章は送らない。"""
from __future__ import annotations

import calendar
import json
import re
import sqlite3
from datetime import date, timedelta

from keika.files import DocRef, scan
from keika.findings import Evidence, Finding
from keika.ledger import User, get_user, list_users, squash
from keika.reading import DocFacts, Source, stored_facts
from keika.records import Record, list_records
from keika.rules import active_rules
from keika.settings import Settings, today

HOME_VISIT = "居宅訪問"
PLAN_TYPES = ("居宅サービス計画書", "介護予防サービス計画書")
REQUIRED_DOCS = ("アセスメント", "サービス担当者会議の要点")
NUDGE_DAYS = 60
COMMITTEE_LIMIT_DAYS = 200  # おおむね6か月（老企第22号）。少しの幅を持たせる


def _month(d: str) -> str:
    return d[:7]


def _add_months(m: str, n: int = 1) -> str:
    total = int(m[:4]) * 12 + int(m[5:7]) - 1 + n
    return f"{total // 12}-{total % 12 + 1:02d}"


def _months(first: str, last: str) -> list[str]:
    out = []
    while first <= last:
        out.append(first)
        first = _add_months(first)
    return out


def _parse(d: str | None) -> date | None:
    try:
        return date.fromisoformat(d) if d else None
    except ValueError:
        return None


def _record_evidence(r: Record) -> Evidence:
    file = r.source.removeprefix("csv:") if r.source.startswith("csv:") else "支援経過（アプリの記録）"
    return Evidence(file=file, record_date=r.event_date, quote=r.body[:60])


def _doc_evidence(rel: str, source: Source | None) -> Evidence:
    return Evidence(file=rel, page=source.page if source else None, quote=source.quote if source else None)


def _is_yobo(user: User) -> bool:
    return "予防" in str(user.data.get("type", ""))


def _latest_plan(facts: list[tuple[DocRef, DocFacts]]) -> tuple[DocRef, DocFacts] | None:
    plans = [(r, f) for r, f in facts if f.doc_type in PLAN_TYPES and f.period_start]
    return max(plans, key=lambda p: p[1].period_start) if plans else None


# --- 月1回の訪問（居宅）---


def _monthly_visits(user: User, records: list[Record], on: date) -> list[Finding]:
    if not records:
        return []
    out = []
    this_month = on.isoformat()[:7]
    for month in _months(_month(records[0].event_date), this_month):
        rows = [r for r in records if _month(r.event_date) == month]
        if any(r.kind_read == HOME_VISIT for r in rows):
            continue
        scope = f"{month} の支援経過"
        if month == this_month:
            left = calendar.monthrange(on.year, on.month)[1] - on.day
            out.append(Finding(rule_id="R06", user_id=user.id, kind="nudge", period=month, searched_scope=scope, todo="今月の居宅訪問",
                               message=f"{user.name}さんの今月の居宅訪問はまだのようです。今月はあと{left}日です。"))
        elif any(r.kind_read is None for r in rows):
            out.append(Finding(rule_id="R06", user_id=user.id, kind="unknown", period=month, searched_scope=scope,
                               evidence=[_record_evidence(r) for r in rows if r.kind_read is None],
                               message=f"{month}の記録に、訪問か電話かなどを読み取れないものがあります。家を訪ねた記録か確認してください。"))
        else:
            written = [r for r in rows if "訪問" in r.kind_written]
            detail = "".join(f"{r.event_date} は種別が「{r.kind_written}」ですが、本文は「{r.kind_read}」と読めます。"
                             for r in written if r.kind_read != r.kind_written)
            out.append(Finding(rule_id="R06", user_id=user.id, kind="finding", period=month, searched_scope=scope, todo=f"{month}の居宅訪問の記録を確かめる",
                               evidence=[_record_evidence(r) for r in (written or rows)],
                               message=f"{month}に居宅を訪問しての面接の記録が見当たりません。{detail}"
                                       "特段の事情があれば、その記録があるか確認してください。"))
    return out


def _pending_visits(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """記録の確認で待っている話のうち、この利用者の居宅訪問の日。"""
    days = []
    for row in conn.execute("SELECT entry, warnings FROM review_items WHERE status = 'open'"):
        entry = json.loads(row["entry"])
        if HOME_VISIT in json.loads(row["warnings"]):  # 訪問だと AI が読んだだけで、話に訪問の語が無い
            continue
        if entry.get("user_id") == user_id and entry.get("kind_read") == HOME_VISIT and entry.get("event_date"):
            days.append(entry["event_date"])
    return days


def _waiting_in_review(visits: list[Finding], days: list[str]) -> list[Finding]:
    """話はあるのに「まだ」と言わない。確認待ちの話に訪問があれば、そちらを片付けるよう知らせる。"""
    out = []
    for f in visits:
        first, _, last = f.period.partition("〜")
        day = next((d for d in days if first and first <= d[:7] <= (last or first)), None)
        if day and f.kind == "nudge":
            f = f.model_copy(update={"todo": "訪問の話を記録にする", "evidence": [],
                                     "message": f"{day} の訪問の話が「記録の確認」で待っています。確かめて記録にしてください。"})
        elif day and f.kind == "finding":  # 別の食い違いを消さない。待っている話があることだけ足す
            f = f.model_copy(update={"message": f"{f.message}{day} の訪問の話が「記録の確認」で待っています。"})
        out.append(f)
    return out


# --- 予防の3か月 ---


def _yobo_visits(user: User, records: list[Record], facts: list[tuple[DocRef, DocFacts]], on: date) -> list[Finding]:
    if not records:
        return []
    plan = _latest_plan(facts)
    if plan is None:
        return [Finding(rule_id="R06", user_id=user.id, kind="unknown", searched_scope="読み取った計画書",
                        message="計画の開始日が分からないので、3か月ごとの訪問の期間を計算できません。計画書を確認してください。")]
    first_record = _month(records[0].event_date)
    this_month = on.isoformat()[:7]
    out = []
    start = _add_months(_month(plan[1].period_start))  # サービス開始月の翌月から数える
    while start <= this_month:
        last = _add_months(start, 2)
        period = f"{start}〜{last}"
        rows = [r for r in records if start <= _month(r.event_date) <= last]
        scope = f"{period} の支援経過"
        if any(r.kind_read == HOME_VISIT for r in rows):
            pass
        elif last == this_month:
            out.append(Finding(rule_id="R06", user_id=user.id, kind="nudge", period=period, searched_scope=scope, todo="3か月に1回の訪問",
                               message=f"{user.name}さんの3か月に1回の訪問は、{period} の分がまだのようです。今月が最後の月です。"))
        elif last > this_month:
            pass
        elif start < first_record:
            out.append(Finding(rule_id="R06", user_id=user.id, kind="unknown", period=period, searched_scope=scope,
                               message=f"{period} の訪問は、この期間の前半の記録が取り込まれていないので分かりません。確認してください。"))
        else:
            out.append(Finding(rule_id="R06", user_id=user.id, kind="finding", period=period, searched_scope=scope, todo=f"{period}の居宅訪問の記録を確かめる",
                               evidence=[_record_evidence(r) for r in rows],
                               message=f"{period}（3か月に1回）に居宅を訪問しての面接の記録が見当たりません。"
                                       "特段の事情があれば、その記録があるか確認してください。"))
        start = _add_months(start, 3)
    return out


# --- 署名、同意、先回り ---


def _signatures(user: User, facts: list[tuple[DocRef, DocFacts]], on: date) -> list[Finding]:
    out = []
    this_month = on.isoformat()[:7]
    for ref, f in facts:
        if f.doc_type != "利用票" or not f.target_month or f.target_month > this_month:
            continue
        ev = [_doc_evidence(ref.rel_path, f.signature_source)]
        if f.signature_present is False:
            out.append(Finding(rule_id="R13", user_id=user.id, kind="finding", period=f.target_month, evidence=ev, todo=f"{f.target_month}の利用票の署名を確かめる",
                               message=f"{f.target_month}の利用票の署名欄が空のようです。署名をいただいているか確認してください。"))
        elif f.signature_present is None:
            out.append(Finding(rule_id="R13", user_id=user.id, kind="unknown", period=f.target_month, evidence=ev,
                               message=f"{f.target_month}の利用票の署名の有無を読み取れませんでした。原本を確認してください。"))
    return out


def _consent(user: User, facts: list[tuple[DocRef, DocFacts]]) -> list[Finding]:
    plan = _latest_plan(facts)
    if plan is None:
        return []
    ref, f = plan
    ev = _doc_evidence(ref.rel_path, f.consent_source or f.period_source)
    if not f.consent_date:
        return [Finding(rule_id="R10", user_id=user.id, kind="unknown", period=f.period_start, evidence=[ev],
                        message="計画書の同意日を読み取れませんでした。同意日と署名を確認してください。")]
    if f.consent_date > f.period_start:
        return [Finding(rule_id="R10", user_id=user.id, kind="finding", period=f.period_start, evidence=[ev], todo="説明と同意の順番を確かめる",
                        message=f"計画の開始日 {f.period_start} より後の {f.consent_date} が同意日になっているようです。"
                                "説明と同意の順番を確認してください。")]
    return []


def _plan_expiry(user: User, facts: list[tuple[DocRef, DocFacts]], on: date) -> list[Finding]:
    plans = [(r, f) for r, f in facts if f.doc_type in PLAN_TYPES and _parse(f.period_end)]
    if not plans:
        return []
    ref, f = max(plans, key=lambda p: p[1].period_end)
    left = (_parse(f.period_end) - on).days
    if left > NUDGE_DAYS:
        return []
    if left < 0:
        return [Finding(rule_id="R45", user_id=user.id, kind="finding", period=f.period_end, evidence=[_doc_evidence(ref.rel_path, f.period_source)], todo="次の計画を確かめる",
                        message=f"読み取った一番新しい計画の期間は {f.period_end} までで、過ぎています。次の計画があるか確認してください。")]
    return [Finding(rule_id="R45", user_id=user.id, kind="nudge", period=f.period_end, evidence=[_doc_evidence(ref.rel_path, f.period_source)], todo="計画の見直しの準備",
                    message=f"計画の期間が {f.period_end} で終わります。あと{left}日です。見直しの準備を確認してください。")]


def _cert_renewal(user: User, on: date) -> list[Finding]:
    cert = user.data.get("cert_period") or []
    end = _parse(cert[1]) if len(cert) == 2 else None
    if end is None or (end - on).days > NUDGE_DAYS:
        return []
    if end < on:
        return [Finding(rule_id="R44", user_id=user.id, kind="finding", period=end.isoformat(), searched_scope="台帳の認定有効期間", todo="認定の更新を確かめる",
                        message=f"台帳の認定の有効期間 {end.isoformat()} を過ぎています。更新の結果と台帳を確認してください。")]
    deadline = end - timedelta(days=30)
    timing = f"30日前の {deadline.isoformat()} を過ぎています。更新の申請の状況" if on > deadline else f"{deadline.isoformat()} までに更新の申請がされるよう、援助"
    return [Finding(rule_id="R44", user_id=user.id, kind="nudge", period=end.isoformat(), searched_scope="台帳の認定有効期間", todo="認定の更新申請の援助",
                    message=f"認定の有効期間が {end.isoformat()} で終わります。{timing}を確認してください。")]


_MEETING_IN_TEXT = re.compile(r"担当者会議[：:\s]*(\d{4}-\d{2}-\d{2})")


def _meetings_in_text(s: Settings, facts: list[tuple[DocRef, DocFacts]], on: date) -> list[tuple[str, DocRef]]:
    """読み取りが拾わなかった開催の記載を、書類の文字から探す。"""
    from keika.claude_checks import _ref_text  # 循環を避ける

    found = []
    for ref, _ in facts:
        for day in _MEETING_IN_TEXT.findall(_ref_text(s, ref) or ""):
            if (_parse(day) or on) <= on:  # 先の予定は開催の記載ではない
                found.append((day, ref))
    return found


def _missing_docs(s: Settings, user: User, refs: list[DocRef], facts: list[tuple[DocRef, DocFacts]], on: date) -> list[Finding]:
    if not facts:
        return []  # まだ読み取っていない書類を「無い」と言わない
    folder = "/".join(refs[0].rel_path.split("/")[:2])
    scope = f"{folder}/ の読み取り済みの書類"
    types = {f.doc_type for _, f in facts}
    missing = [t for t in REQUIRED_DOCS if t not in types]
    if not missing:
        return []
    if len(facts) < len(refs):
        return [Finding(rule_id="R33", user_id=user.id, kind="unknown", searched_scope=scope,
                        message=f"まだ読み取っていない書類が {len(refs) - len(facts)} 件あるので、{'・'.join(missing)}の有無はまだ分かりません。")]
    meetings = [(a.date, ref) for ref, f in facts for a in f.activities if "担当者会議" in a.title and a.date] or _meetings_in_text(s, facts, on)
    found = []
    for t in missing:
        if t == "サービス担当者会議の要点" and meetings:  # 他の書類に開催の記載があれば「無い」とは言わない
            day, ref = meetings[-1]
            found.append(Finding(rule_id="R33", user_id=user.id, kind="unknown", searched_scope=scope,
                                 message=f"要点の書類は見当たりませんが、{ref.rel_path.split('/')[-1]} に {day} の担当者会議の記載があります。要点として足りるか確認してください。"))
        else:
            found.append(Finding(rule_id="R33", user_id=user.id, kind="finding", period=t, searched_scope=scope, todo=f"{t}を確かめる",
                                 message=f"ドライブの {folder}/ に{t}が見当たりません。別の場所にあれば確認してください。"))
    return found


# --- 事業所 ---


def _committee_dates(facts: list[tuple[DocRef, DocFacts]], topic: str) -> list[tuple[str, DocRef, DocFacts]]:
    found: dict[str, tuple[str, DocRef, DocFacts]] = {}
    for ref, f in facts:
        for act in f.activities:
            if "委員会" in act.kind + act.title and re.search(topic, act.title) and _parse(act.date):
                # 議事録そのものを、一覧表より根拠として優先する
                if act.date not in found or f.doc_type == "委員会の記録":
                    found[act.date] = (act.date, ref, f)
    return [found[d] for d in sorted(found)]


def _infection_committee(facts: list[tuple[DocRef, DocFacts]], on: date) -> list[Finding]:
    rows = _committee_dates(facts, "感染")
    out = []
    dates = [_parse(d) for d, _, _ in rows]
    for (d0, ref, f), a, b in zip(rows, dates, dates[1:] + [on]):
        if (b - a).days > COMMITTEE_LIMIT_DAYS:
            until = "今日" if b == on else b.isoformat()
            out.append(Finding(rule_id="R24", user_id=None, kind="finding", period=d0, evidence=[_doc_evidence(ref.rel_path, f.doc_date_source)], todo="感染症の委員会の開催を確かめる",
                               message=f"感染症対策の委員会は {d0} の次の記録が、{until}までの間に見当たりません（おおむね6か月に1回）。開催と記録を確認してください。"))
    return out


# --- まとめ ---


def user_refs(conn: sqlite3.Connection, s: Settings, user: User) -> list[DocRef]:
    target = squash(user.name)
    same_name = sum(squash(u.name) == target for u in list_users(conn)) > 1

    def mine(folder: str) -> bool:
        if squash(re.sub(r"^[A-Za-z]+\d+_", "", folder)) != target:
            return False
        # 同姓同名がいる時は、ID の付いたフォルダだけを本人のものとする（他人の書類を読まない）
        return folder.startswith(f"{user.id}_") if same_name else True

    return [r for r in scan(s) if r.user_folder and mine(r.user_folder)]


def run_machine_checks(conn: sqlite3.Connection, s: Settings, user_id: str | None = None) -> list[Finding]:
    on = today(s)
    allowed = {r.id for r in active_rules(on)}
    users = [u for u in [get_user(conn, user_id)] if u] if user_id else list_users(conn)
    out: list[Finding] = []
    for user in users:
        refs = user_refs(conn, s, user)
        facts = stored_facts(conn, refs)
        records = list_records(conn, user.id)
        visits = _yobo_visits(user, records, facts, on) if _is_yobo(user) else _monthly_visits(user, records, on)
        visits = _waiting_in_review(visits, _pending_visits(conn, user.id))
        out += visits + _signatures(user, facts, on) + _consent(user, facts) + _plan_expiry(user, facts, on) + _cert_renewal(user, on)
        if refs:
            out += _missing_docs(s, user, refs, facts, on)
    if user_id is None:
        office = stored_facts(conn, [r for r in scan(s) if r.scope == "office"])
        out += _infection_committee(office, on)
    return [f for f in out if f.rule_id in allowed]
