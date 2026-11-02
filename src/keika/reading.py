"""書類の読み取りと保存。

機械の点検はここで保存した項目に対して走るので、項目の形をここで固定する。
同じ中身のファイルは読み直さない（費用と待ち時間の対策）。
"""
from __future__ import annotations

import argparse
import base64
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from pydantic import BaseModel, Field

from keika.claude import METER, StructuredError, ask_structured, make_client
from keika.files import DocRef, Payload, scan, to_payload
from keika.settings import Settings, load_settings, now_real

DocType = Literal[
    "アセスメント", "居宅サービス計画書", "介護予防サービス計画書", "サービス担当者会議の要点", "利用票",
    "契約書", "重要事項説明書", "個人情報使用同意書", "説明の記録", "主治医意見書", "モニタリング", "評価表",
    "支援経過", "お知らせ", "指針・マニュアル", "業務継続計画", "委員会の記録", "研修の記録", "規程",
    "掲示", "指定・保険", "誓約書", "口述・文字起こし", "その他",
]


class Source(BaseModel):
    page: int | None = Field(default=None, description="PDF のページ番号（1始まり）。ページの無い書類は null")
    quote: str = Field(description="原文からそのまま抜き出した短い語句。要約しない")


class TextFact(BaseModel):
    text: str
    source: Source | None = None


class ServiceFact(BaseModel):
    name: str = Field(description="サービスの種類や事業所名")
    per_week: float | None = Field(default=None, description="週あたりの回数。書かれていなければ null")
    days: str | None = Field(default=None, description="曜日や頻度の書きぶりをそのまま")
    source: Source | None = None


class ActivityFact(BaseModel):
    kind: str = Field(description="委員会、研修、訓練、会議など")
    date: str | None = Field(default=None, description="実施日 YYYY-MM-DD")
    title: str
    participants: str | None = None
    source: Source | None = None


class DocFacts(BaseModel):
    readable: bool = Field(default=True, description="中身を読めたら true")
    read_note: str | None = Field(default=None, description="読めなかった部分や迷った点")
    doc_type: DocType = "その他"
    body_status: Literal["本文あり", "表紙や目次だけ", "白紙", "不明"] = Field(
        default="不明", description="中身が実際に書かれているか。ファイルがあるだけで本文が無いものを見分ける"
    )
    summary: str = Field(default="", description="何の書類かを一文で")
    user_name: str | None = Field(default=None, description="対象の利用者の氏名")
    doc_date: str | None = Field(default=None, description="作成日または実施日 YYYY-MM-DD")
    doc_date_source: Source | None = None
    period_start: str | None = Field(default=None, description="計画期間などの始まり YYYY-MM-DD")
    period_end: str | None = Field(default=None, description="計画期間などの終わり YYYY-MM-DD")
    period_source: Source | None = None
    target_month: str | None = Field(default=None, description="利用票など月ごとの書類の対象月 YYYY-MM")
    signature_present: bool | None = Field(
        default=None, description="利用者または代理人の署名・押印欄に記入があれば true、欄が空なら false、欄が無い・判断できないなら null"
    )
    signature_date: str | None = Field(default=None, description="署名・交付・同意の日付 YYYY-MM-DD")
    signature_source: Source | None = None
    consent_date: str | None = Field(default=None, description="計画などへの同意の日付 YYYY-MM-DD")
    consent_source: Source | None = None
    consent_persons: list[TextFact] = Field(default_factory=list, description="同意した人（本人、家族など）")
    issues: list[TextFact] = Field(default_factory=list, description="課題、ニーズ")
    goals: list[TextFact] = Field(default_factory=list, description="目標")
    services: list[ServiceFact] = Field(default_factory=list, description="サービスと頻度")
    delivered_to: list[TextFact] = Field(default_factory=list, description="交付先")
    attendees: list[TextFact] = Field(default_factory=list, description="会議の出席者、照会先")
    activities: list[ActivityFact] = Field(default_factory=list, description="記録されている委員会・研修・訓練など")

    @classmethod
    def unknown(cls, note: str) -> "DocFacts":
        return cls(readable=False, read_note=note, summary="読めなかった書類")


SYSTEM = """あなたは日本の介護支援専門員（ケアマネジャー）の事業所で、書類の読み取りを担当します。
渡された書類を1件読み、決まった形で項目を埋めてください。

守ること:
- 書類に書かれていることだけを書く。推測で埋めない。書かれていない項目は null か空のリストにする。
- 様式は事業所ごとに違う。見出しの名前に頼らず、中身で判断する。
- source.quote は原文からそのまま抜き出す。要約や言い換えをしない。PDF はページ番号も入れる。
- 日付は YYYY-MM-DD に直す。和暦は西暦に直す（令和8年は2026年）。
- 署名・押印は目で確かめる。印字された氏名は署名ではない。署名欄が空なら signature_present は false。
  欄が無い、または読み取れず判断できない時は null にして、read_note に理由を書く。
- 表紙や目次、見出しだけで本文の無い書類は body_status を「表紙や目次だけ」にする。
- 複数の日付や版がある時は、書類が示す通りに各項目へ入れ、迷いは read_note に書く。
- 良し悪しの判定はしない。読み取りだけを行う。"""


def _content(ref: DocRef, payload: Payload) -> list[dict]:
    note = {"type": "text", "text": f"ファイル名: {ref.rel_path}\nこの書類の項目を埋めてください。"}
    if payload.text is not None:
        note["text"] = f"ファイル名: {ref.rel_path}\n--- 書類の中身 ---\n{payload.text}\n--- ここまで ---\nこの書類の項目を埋めてください。"
        return [note]
    data = base64.standard_b64encode(payload.data).decode("ascii")
    source = {"type": "base64", "media_type": payload.media_type, "data": data}
    kind = "document" if payload.media_type == "application/pdf" else "image"
    return [{"type": kind, "source": source}, note]


def _cached(conn: sqlite3.Connection, ref: DocRef) -> DocFacts | None:
    row = conn.execute("SELECT facts FROM doc_facts WHERE sha256 = ?", (ref.sha256,)).fetchone()
    return DocFacts.model_validate_json(row["facts"]) if row else None


def _save(conn: sqlite3.Connection, ref: DocRef, facts: DocFacts) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO doc_facts (sha256, rel_path, facts, read_at) VALUES (?, ?, ?, ?)",
        (ref.sha256, ref.rel_path, facts.model_dump_json(), now_real().isoformat()),
    )
    conn.commit()


def _ask(client: Any, s: Settings, ref: DocRef) -> tuple[DocFacts, bool]:
    """(項目, 保存してよいか) を返す。通信の失敗などは保存せず、次の機会に読み直す。"""
    payload = to_payload(s, ref)
    if payload.unreadable:
        return DocFacts.unknown(f"開けませんでした（{payload.reason}）"), True
    try:
        return ask_structured(client, SYSTEM, _content(ref, payload), DocFacts), True
    except StructuredError as e:
        return DocFacts.unknown(str(e)), False


def read_doc(conn: sqlite3.Connection, client: Any, s: Settings, ref: DocRef) -> DocFacts:
    return read_docs(conn, client, s, [ref])[0][1]


def read_docs(
    conn: sqlite3.Connection, client: Any, s: Settings, refs: list[DocRef], workers: int = 4
) -> list[tuple[DocRef, DocFacts]]:
    """未読の書類だけを並行に読む。保存は呼び出し元のスレッドで行う。"""
    found = {r.sha256: f for r in refs if (f := _cached(conn, r)) is not None}
    todo = list({r.sha256: r for r in refs if r.sha256 not in found}.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ref, (facts, keep) in zip(todo, pool.map(lambda r: _ask(client, s, r), todo)):
            found[ref.sha256] = facts
            if keep:
                _save(conn, ref, facts)
    return [(r, found[r.sha256]) for r in refs]


def stored_facts(conn: sqlite3.Connection, refs: list[DocRef]) -> list[tuple[DocRef, DocFacts]]:
    """保存済みの読み取り結果だけを返す（Claude を呼ばない）。"""
    return [(r, f) for r in refs if (f := _cached(conn, r)) is not None]


def _describe(facts: DocFacts) -> str:
    lines = [f"  種類: {facts.doc_type}（{facts.body_status}） {facts.summary}"]
    for label, value in (
        ("日付", facts.doc_date), ("期間", f"{facts.period_start}〜{facts.period_end}" if facts.period_start else None),
        ("対象月", facts.target_month), ("署名", facts.signature_present), ("同意日", facts.consent_date),
        ("読めない点", facts.read_note),
    ):
        if value is not None:
            lines.append(f"  {label}: {value}")
    for sv in facts.services:
        lines.append(f"  サービス: {sv.name} 週{sv.per_week} {sv.days or ''}")
    for issue in facts.issues:
        lines.append(f"  課題: {issue.text}")
    for act in facts.activities:
        lines.append(f"  {act.kind}: {act.date} {act.title}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive の書類を読み取って保存する")
    parser.add_argument("--user", help="利用者フォルダの先頭（例: U04）。省略すると全部")
    parser.add_argument("--scope", choices=["user", "office"], help="読む範囲")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    from keika.db import connect

    s = load_settings()
    conn = connect(s)
    refs = [r for r in scan(s) if r.scope != "inbox"]
    if args.scope:
        refs = [r for r in refs if r.scope == args.scope]
    if args.user:
        refs = [r for r in refs if r.user_folder and r.user_folder.startswith(args.user)]
    started = time.time()
    results = read_docs(conn, make_client(), s, refs)
    for ref, facts in results:
        print(ref.rel_path)
        print(_describe(facts))
    print(f"\n{len(results)} 件、{time.time() - started:.0f} 秒、API {METER.calls} 回、約 ${METER.cost_usd:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
