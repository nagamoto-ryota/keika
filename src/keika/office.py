"""事業所の宿題。ファイルの有無で済みにしない（本文の無い下書きは未作成と同じ）。

読んでいないものは「まだ」と言わず「分からない」にする。Drive に証拠が出ない項目は人が申告でき、
申告は機械の「済み」と見分けがつくように出す。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from keika.files import DocRef, scan, to_payload
from keika.findings import Evidence, Finding
from keika.ledger import list_people, squash
from keika.machine_checks import _committee_dates, _infection_committee, _parse
from keika.reading import DocFacts, stored_facts
from keika.rules import Rule, active_rules
from keika.settings import Settings, now_real, today

YEAR = timedelta(days=366)
SCOPE = "事業所/ の読み取り済みの書類"
Doc = tuple[DocRef, DocFacts]


@dataclass
class OfficeRow:
    rule: Rule
    status: str  # done / todo / unknown / declared
    evidence_rel_path: str | None = None
    next_step: str = ""
    note: str = ""
    declared_at: str | None = None
    quote: str | None = None


def declare(conn: sqlite3.Connection, rule_id: str, who: str, basis: str) -> None:
    """Drive に証拠が出ない項目を人が「済み」と申告する。誰がいつ何を根拠にしたかを残す。"""
    if not who.strip() or not basis.strip():
        raise ValueError("誰が、何を根拠に確かめたかを書いてください")
    conn.execute(
        "INSERT INTO office_declarations (rule_id, who, basis, at) VALUES (?, ?, ?, ?)",
        (rule_id, who.strip(), basis.strip(), now_real().isoformat()),
    )
    conn.commit()


def _about(doc: Doc, pattern: str) -> bool:
    ref, f = doc
    return bool(re.search(pattern, ref.rel_path + f.summary))


def _real(doc: Doc) -> bool:
    return doc[1].body_status == "本文あり"


def _activities(docs: list[Doc], kind: str, topic: str):
    for ref, f in docs:
        if _about((ref, f), "計画") and f.doc_type != "業務継続計画":  # 研修計画は予定で、実施の記録ではない
            continue
        for act in f.activities:
            if (kind == act.kind or (act.kind != "委員会" and kind in act.title)) and re.search(topic, act.title + act.kind) and _parse(act.date):  # 委員会の題の「研修の予定」は実施ではない
                yield act, ref


def _within_year(docs: list[Doc], kind: str, topic: str, on: date) -> bool:
    return any(on - _parse(a.date) <= YEAR for a, _ in _activities(docs, kind, topic))


def _yearly_step(docs: list[Doc], label: str, kind: str, topic: str) -> str:
    """年1回の活動が切れている時の案内。前回があれば、いつで切れたかも伝える。"""
    days = [a.date for a, _ in _activities(docs, kind, topic)]
    if days:
        return f"{label}の{kind}は最後が {max(days, key=_parse)} で、1年を超えています。年1回以上行って記録する"
    return f"{label}の{kind}を年1回以上行って記録する"


def _guide(docs: list[Doc], pattern: str) -> Doc | None:
    hits = [d for d in docs if _about(d, pattern) and d[1].doc_type not in ("委員会の記録", "研修の記録")]
    return next((d for d in hits if _real(d)), hits[0] if hits else None)


def _bcp(docs: list[Doc], on: date) -> dict:
    plans = [d for d in docs if d[1].doc_type == "業務継続計画" or _about(d, "業務継続|BCP")]
    plans = [d for d in plans if d[1].doc_type not in ("研修の記録", "委員会の記録")]
    infection = next((d for d in plans if _about(d, "感染") and _real(d)), None)
    disaster = next((d for d in plans if _about(d, "災害") and _real(d)), None)
    draft = next((d for d in plans if _about(d, "災害") and not _real(d)), None)
    steps = []
    if not infection:
        steps.append("感染症編の計画を作る")
    if not disaster:
        steps.append("災害編は表紙や目次だけのようです。本文を作る" if draft else "災害編の計画を作る")
    for kind in ("研修", "訓練"):
        if not _within_year(docs, kind, "業務継続|BCP", on):
            steps.append(_yearly_step(docs, "BCP", kind, "業務継続|BCP"))
    first = infection or disaster or draft
    return {"steps": steps, "evidence": draft if draft and not disaster else first}


def _infection(docs: list[Doc], on: date) -> dict:
    guide = _guide(docs, "感染")
    steps = []
    if not guide or not _real(guide):
        steps.append("感染症の指針を整える")
    gaps = _infection_committee(docs, on)
    if gaps:
        steps.extend(g.message.removesuffix("開催と記録を確認してください。").rstrip("。") for g in gaps)
    elif not _committee_dates(docs, "感染"):
        steps.append("感染症対策の委員会をおおむね6か月に1回開いて記録する")
    for kind in ("研修", "訓練"):
        if not _within_year(docs, kind, "感染", on):
            steps.append(_yearly_step(docs, "感染症", kind, "感染"))
    rows = _committee_dates(docs, "感染")
    return {"steps": steps, "evidence": rows[-1][1:] if rows else guide}


def _regulation_mentions(s: Settings, docs: list[Doc], word: str) -> bool | None:
    regs = [d for d in docs if d[1].doc_type == "規程" or _about(d, "運営規程")]
    if not regs:
        return None
    payload = to_payload(s, regs[0][0])
    return None if payload.text is None else word in payload.text


def _abuse(conn: sqlite3.Connection, s: Settings, docs: list[Doc], on: date) -> dict:
    guide = _guide(docs, "虐待")
    steps = []
    if not guide or not _real(guide):
        steps.append("虐待防止の指針を整える")
    if not _committee_dates(docs, "虐待"):
        steps.append("虐待防止の委員会を定期的に開いて記録する")
    if not _within_year(docs, "研修", "虐待", on):
        steps.append(_yearly_step(docs, "虐待防止", "研修", "虐待"))
    if _regulation_mentions(s, docs, "虐待") is False:
        steps.append("運営規程に虐待防止の措置を書く")
    trainings = list(_activities(docs, "研修", "虐待"))
    record = next((ref for _, ref in _activities(docs, "研修", ".")), None)
    for person in list_people(conn, "staff"):
        hired = _parse(_staff_data(conn, person.id).get("hired"))
        if hired is None or on - hired > YEAR:
            continue
        family = person.name.split()[0]
        attended = any(_parse(a.date) >= hired and family in (a.participants or "") + a.title for a, _ in trainings)
        if not attended:
            steps.append(f"{person.name}さん（{hired.isoformat()} 入職）の新規採用時の虐待防止研修は、入職後に受けた記録が見当たりません。受講を確かめる")
    records_first = next((d for d in docs if record and d[0].rel_path == record.rel_path), None)
    return {"steps": steps, "evidence": records_first if any("研修" in x for x in steps) and records_first else guide}


def _staff_data(conn: sqlite3.Connection, staff_id: str) -> dict:
    row = conn.execute("SELECT data FROM people WHERE id = ?", (staff_id,)).fetchone()
    return json.loads(row["data"]) if row else {}


def _pledges(conn: sqlite3.Connection, docs: list[Doc]) -> dict:
    pledges = [d for d in docs if d[1].doc_type == "誓約書" or _about(d, "誓約")]
    text = " ".join(squash(d[0].rel_path + (d[1].user_name or "")) for d in pledges)
    missing = [p.name for p in list_people(conn, "staff") if squash(p.name.split()[0]) not in text]
    return {"steps": [f"{name}さんの秘密保持の誓約書を確かめる" for name in missing], "evidence": pledges[0] if pledges else None}


def _plain_doc(docs: list[Doc], pattern: str, step: str) -> dict:
    doc = _guide(docs, pattern)
    return {"steps": [] if doc and _real(doc) else [step], "evidence": doc}


def _accident(docs: list[Doc], on: date) -> dict:
    manual = _guide(docs, "事故")
    insurance = next((d for d in docs if _about(d, "賠償|保険")), None)
    steps = []
    if not manual or not _real(manual):
        steps.append("事故対応のマニュアルを整える")
    if insurance is None:
        steps.append("損害賠償保険の加入証を確かめる")
    else:
        end = _parse(insurance[1].period_end)
        if end and end < on:
            steps.append("損害賠償保険の期限が切れていないか確かめる")
    return {"steps": steps, "evidence": manual or insurance}


def office_rows(conn: sqlite3.Connection, s: Settings) -> list[OfficeRow]:
    on = today(s)
    refs = [r for r in scan(s) if r.scope == "office"]
    docs = stored_facts(conn, refs)
    any_read = bool(docs)
    decl = {r["rule_id"]: r for r in conn.execute("SELECT * FROM office_declarations ORDER BY id")}
    rows = []
    for rule in active_rules(on):
        if rule.layer != "事業所":
            continue
        if not any_read:
            rows.append(OfficeRow(rule, "unknown", next_step="事業所の書類をまだ読み取っていません"))
            continue
        result = {
            "R23": lambda: _bcp(docs, on),
            "R24": lambda: _infection(docs, on),
            "R25": lambda: _abuse(conn, s, docs, on),
            "R28": lambda: _plain_doc(docs, "ハラスメント", "ハラスメント防止の方針と相談窓口を決める"),
            "R31": lambda: _pledges(conn, docs),
            "R32": lambda: _accident(docs, on),
        }
        if rule.id == "R27":
            rows.append(_web_posting(rule, docs, decl.get("R27")))
            continue
        if rule.id not in result:
            continue
        outcome = result[rule.id]()
        evidence = outcome["evidence"]
        rel = evidence[0].rel_path if evidence else None
        rows.append(OfficeRow(rule, "todo" if outcome["steps"] else "done", rel, "。".join(outcome["steps"])))
    return rows


def _web_posting(rule: Rule, docs: list[Doc], declared: sqlite3.Row | None) -> OfficeRow:
    posted = next((d for d in docs if d[1].doc_type == "掲示" or _about(d, "掲示")), None)
    rel = posted[0].rel_path if posted else None
    if declared:
        note = f"{declared['who']} が申告: {declared['basis']}"
        return OfficeRow(rule, "declared", rel, note=note, declared_at=declared["at"])
    return OfficeRow(rule, "unknown", rel, "ウェブサイトへの掲載は Drive では分かりません。ご自身で確かめて申告してください")


def office_findings(conn: sqlite3.Connection, s: Settings) -> list[Finding]:
    """やること一覧と測定に渡す形。"""
    out = []
    for row in office_rows(conn, s):
        if row.status not in ("todo", "unknown"):
            continue
        evidence = [Evidence(file=row.evidence_rel_path)] if row.evidence_rel_path else []
        kind = "finding" if row.status == "todo" else "unknown"
        out.append(Finding(
            rule_id=row.rule.id, user_id=None, kind=kind, period="", todo=row.next_step.split("。")[0],
            message=f"{row.next_step}。確認してください。", evidence=evidence, searched_scope=None if evidence else SCOPE,
        ))
    return out
