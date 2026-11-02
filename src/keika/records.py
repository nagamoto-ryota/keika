"""支援経過と変更履歴。

普通に直せる。直すと、いつ誰がどう変えたかが残り、履歴は消せない。
記録は「出来事の日」と「最初に記入した日時」を別に持つ。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from keika.claude import StructuredError, ask_structured
from keika.files import DocRef, parse_csv, to_payload
from keika.ledger import list_users, squash
from keika.settings import Settings, now_real

KindRead = Literal["居宅訪問", "事業所訪問", "電話", "来所", "会議", "文書・FAX・メール", "その他", "不明"]
EDITABLE = {"user_id", "event_date", "event_time", "kind_written", "kind_read", "monitoring", "partner", "body", "author"}


@dataclass(frozen=True)
class RecordIn:
    user_id: str
    event_date: str
    body: str
    event_time: str = ""
    kind_written: str = ""
    kind_read: str | None = None
    monitoring: bool | None = None
    partner: str = ""
    author: str = ""
    source: str = "manual"


@dataclass(frozen=True)
class Record:
    id: int
    user_id: str
    event_date: str
    event_time: str
    kind_written: str
    kind_read: str | None
    monitoring: bool | None
    partner: str
    body: str
    author: str
    first_written_at: str | None
    source: str
    unchecked: bool


@dataclass(frozen=True)
class Version:
    version: int
    user_id: str
    event_date: str
    event_time: str
    kind_written: str
    kind_read: str | None
    monitoring: bool | None
    partner: str
    body: str
    author: str
    unchecked: bool
    editor: str
    edited_at: str


@dataclass
class ImportResult:
    added: int = 0
    unchanged: int = 0
    changed: list[int] = field(default_factory=list)
    error: str | None = None


def _to_record(row: sqlite3.Row) -> Record:
    return Record(
        id=row["id"], user_id=row["user_id"], event_date=row["event_date"], event_time=row["event_time"],
        kind_written=row["kind_written"], kind_read=row["kind_read"],
        monitoring=None if row["monitoring"] is None else bool(row["monitoring"]),
        partner=row["partner"], body=row["body"], author=row["author"], first_written_at=row["first_written_at"],
        source=row["source"], unchecked=bool(row["unchecked"]),
    )


def _snapshot(conn: sqlite3.Connection, record_id: int, editor: str) -> None:
    r = get_record(conn, record_id)
    data = {k: v for k, v in asdict(r).items() if k not in ("id", "first_written_at", "source")}
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM record_versions WHERE record_id = ?", (record_id,)).fetchone()
    conn.execute(
        "INSERT INTO record_versions (record_id, version, snapshot, editor, edited_at) VALUES (?, ?, ?, ?, ?)",
        (record_id, row["v"] + 1, json.dumps(data, ensure_ascii=False), editor, now_real().isoformat()),
    )


def _insert(conn: sqlite3.Connection, r: RecordIn, written_at: str | None, source_key: str | None, unchecked: bool) -> int:
    cur = conn.execute(
        "INSERT INTO records (user_id, event_date, event_time, kind_written, kind_read, monitoring, partner, body, author,"
        " first_written_at, source, source_key, unchecked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (r.user_id, r.event_date, r.event_time, r.kind_written, r.kind_read,
         None if r.monitoring is None else int(r.monitoring), r.partner, r.body, r.author,
         written_at, r.source, source_key, int(unchecked)),
    )
    _snapshot(conn, cur.lastrowid, r.author or r.source)
    return cur.lastrowid


def add_record(conn: sqlite3.Connection, r: RecordIn) -> Record:
    """記入日時は実際の時計で自動に入る。届いた記録は「未確認」から始まる。"""
    record_id = _insert(conn, r, now_real().isoformat(), None, True)
    conn.commit()
    return get_record(conn, record_id)


def get_record(conn: sqlite3.Connection, record_id: int) -> Record | None:
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    return _to_record(row) if row else None


def list_records(conn: sqlite3.Connection, user_id: str) -> list[Record]:
    rows = conn.execute("SELECT * FROM records WHERE user_id = ? ORDER BY event_date, event_time, id", (user_id,))
    return [_to_record(r) for r in rows]


def edit_record(conn: sqlite3.Connection, record_id: int, changes: dict[str, Any], editor: str) -> Record:
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ValueError(f"この項目は変えられません: {sorted(unknown)}")
    current = get_record(conn, record_id)
    real = {k: v for k, v in changes.items() if getattr(current, k) != v}
    if not real:
        return current
    columns = ", ".join(f"{k} = ?" for k in real)
    conn.execute(f"UPDATE records SET {columns} WHERE id = ?", (*real.values(), record_id))
    _snapshot(conn, record_id, editor)
    conn.commit()
    return get_record(conn, record_id)


def mark_checked(conn: sqlite3.Connection, record_id: int, editor: str) -> Record:
    conn.execute("UPDATE records SET unchecked = 0 WHERE id = ?", (record_id,))
    _snapshot(conn, record_id, editor)
    conn.commit()
    return get_record(conn, record_id)


def history(conn: sqlite3.Connection, record_id: int) -> list[Version]:
    rows = conn.execute("SELECT * FROM record_versions WHERE record_id = ? ORDER BY version", (record_id,))
    return [Version(version=r["version"], editor=r["editor"], edited_at=r["edited_at"], **json.loads(r["snapshot"])) for r in rows]


# --- CSV の取り込み ---


class WireCsvRow(BaseModel):
    row: int = Field(description="データ行の番号（見出しの次の行を1とする）")
    kind_read: KindRead = Field(description="本文から読み取った実際の接触の種類。種別欄ではなく本文で判断する")
    monitoring: bool = Field(description="本文にモニタリングの内容があれば true")
    reason: str = Field(description="そう読んだ理由の短い語句")


class WireCsv(BaseModel):
    date_column: str = Field(description="出来事の日の列名")
    time_column: str = Field(description="時刻の列名。無ければ空文字")
    kind_column: str = Field(description="種別の列名。無ければ空文字")
    partner_column: str = Field(description="相手の列名。無ければ空文字")
    body_column: str = Field(description="本文の列名")
    author_column: str = Field(description="記録者の列名。無ければ空文字")
    rows: list[WireCsvRow]


CSV_SYSTEM = """日本の介護支援専門員の事業所の、介護ソフトから書き出した支援経過の CSV を読みます。
列の形は決まっていません。見出しと中身から、どの列が何かを答えてください。
また各行の本文を読み、実際の接触の種類を判定してください。種別欄に「訪問」とあっても、本文が電話なら「電話」です。
本文で判断できない時は「不明」にします。事実を足さないでください。"""


def _find_user_id(conn: sqlite3.Connection, ref: DocRef) -> str | None:
    if not ref.user_folder:
        return None
    folder = squash(ref.user_folder.split("_", 1)[-1])
    hits = [u.id for u in list_users(conn) if squash(u.name) == folder]
    return hits[0] if len(hits) == 1 else None


def _row_key(user_id: str, fields: tuple[str, ...], seen: dict[tuple[str, ...], int]) -> str:
    # 同じ日時・種別・相手の行が複数あっても別の記録として区別する
    seen[fields] = seen.get(fields, 0) + 1
    raw = "\0".join((user_id, *fields, str(seen[fields])))
    return "csv:" + hashlib.sha256(raw.encode()).hexdigest()


def import_csv(conn: sqlite3.Connection, client: Any, s: Settings, ref: DocRef) -> ImportResult:
    """再取り込みで同じ記録は増やさない。文が変わった行は別の版として残して知らせる。"""
    user_id = _find_user_id(conn, ref)
    if user_id is None:
        return ImportResult(error=f"台帳に利用者が見つかりません: {ref.user_folder}")
    payload = to_payload(s, ref)
    if payload.unreadable or payload.text is None:
        return ImportResult(error=payload.reason or "読めませんでした")
    table = parse_csv(payload.text)
    if not table:
        return ImportResult(error="中身が空です")
    header, data = table[0], table[1:]
    done = conn.execute("SELECT 1 FROM imported_files WHERE sha256 = ?", (ref.sha256,)).fetchone()
    if done:
        return ImportResult(unchanged=len(data))
    prompt = f"ファイル名: {ref.rel_path}\n--- CSV ---\n{payload.text}\n--- ここまで ---"
    try:
        wire = ask_structured(client, CSV_SYSTEM, [{"type": "text", "text": prompt}], WireCsv)
    except StructuredError as e:
        return ImportResult(error=str(e))
    if wire.date_column not in header or wire.body_column not in header:
        return ImportResult(error="日付か本文の列が分かりませんでした")

    def cell(row: list[str], column: str) -> str:
        return row[header.index(column)].strip() if column in header and header.index(column) < len(row) else ""

    kinds = {r.row: r for r in wire.rows}
    result = ImportResult()
    seen: dict[tuple[str, ...], int] = {}
    for number, row in enumerate(data, start=1):
        event_date, body = cell(row, wire.date_column), cell(row, wire.body_column)
        if not event_date or not body:
            continue
        kind = kinds.get(number)
        kind_read = kind.kind_read if kind and kind.kind_read != "不明" else None
        fields = (event_date, cell(row, wire.time_column), cell(row, wire.kind_column), cell(row, wire.partner_column))
        key = _row_key(user_id, fields, seen)
        existing = conn.execute("SELECT id, body FROM records WHERE source_key = ?", (key,)).fetchone()
        if existing is None:
            record = RecordIn(
                user_id=user_id, event_date=event_date, event_time=fields[1], kind_written=fields[2], partner=fields[3],
                body=body, author=cell(row, wire.author_column), kind_read=kind_read,
                monitoring=kind.monitoring if kind else None, source=f"csv:{ref.rel_path}",
            )
            _insert(conn, record, None, key, False)
            result.added += 1
        elif existing["body"] == body:
            result.unchanged += 1
        else:
            conn.execute("UPDATE records SET body = ?, kind_read = ? WHERE id = ?", (body, kind_read, existing["id"]))
            _snapshot(conn, existing["id"], f"csv:{ref.rel_path}")
            result.changed.append(existing["id"])
    conn.execute(
        "INSERT OR REPLACE INTO imported_files (sha256, rel_path, rows, imported_at) VALUES (?, ?, ?, ?)",
        (ref.sha256, ref.rel_path, len(data), now_real().isoformat()),
    )
    conn.commit()
    return result
