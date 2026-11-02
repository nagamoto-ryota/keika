"""指摘への応答。

対応したかどうかは人に押させない。記録を読んで Claude が閉じる（claude_checks の resolve_finding）。
問題なし（的外れ）: 押した1件だけを黙らせる。根拠の書類が変われば、また出る。黙らせた一覧は管理者が見られる。
経緯を記録した: 後から直せない過去の漏れ用。今日の記録へのリンクが必須で、未対応の件数から分ける。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from keika.findings import Finding, fingerprint
from keika.settings import OutsideRootError, Settings, now_real, safe_path

KINDS = ("off_target", "history_noted")


@dataclass
class Item:
    finding: Finding
    fingerprint: str
    evidence_changed: bool = False
    record_id: int | None = None


@dataclass
class Sorted:
    open: list[Item] = field(default_factory=list)
    noted: list[Item] = field(default_factory=list)


def evidence_hash(s: Settings, f: Finding) -> str:
    """根拠の書類の中身の目印。差し替わったら変わる。"""
    parts = []
    for e in f.evidence:
        try:
            path = safe_path(s, e.file)
            parts.append(hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else f"{e.file}:{e.record_date}:{e.quote}")
        except (OutsideRootError, OSError):
            parts.append(f"{e.file}:{e.record_date}:{e.quote}")
    return hashlib.sha256("\0".join(sorted(parts)).encode()).hexdigest()


def _latest_run_id(conn: sqlite3.Connection, user_id: str | None) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM check_runs WHERE user_id IS ?", (user_id,)).fetchone()
    return row["n"]


def respond(conn: sqlite3.Connection, s: Settings, f: Finding, kind: str, record_id: int | None = None, who: str = "") -> None:
    if kind not in KINDS:
        raise ValueError(f"知らない応答です: {kind}")
    if kind == "history_noted" and record_id is None:
        raise ValueError("経緯を記録した時は、今日の記録へのリンクが必要です")
    conn.execute(
        "INSERT INTO finding_responses (fingerprint, user_id, kind, record_id, evidence_hash, run_id, who, at, finding)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fingerprint(f), f.user_id, kind, record_id, evidence_hash(s, f), _latest_run_id(conn, f.user_id), who,
         now_real().isoformat(), f.model_dump_json()),
    )
    conn.commit()


def sort_findings(conn: sqlite3.Connection, s: Settings, user_id: str | None, findings: list[Finding]) -> Sorted:
    latest: dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT * FROM finding_responses WHERE user_id IS ? ORDER BY id", (user_id,)):
        latest[row["fingerprint"]] = row
    out = Sorted()
    for f in findings:
        fp = fingerprint(f)
        row = latest.get(fp)
        if row is None:
            out.open.append(Item(f, fp))
            continue
        changed = row["evidence_hash"] != evidence_hash(s, f)
        if row["kind"] == "history_noted":
            out.noted.append(Item(f, fp, changed, row["record_id"]))
        elif changed:
            out.open.append(Item(f, fp, True))
    return out


def silenced_list(conn: sqlite3.Connection) -> list[dict]:
    """管理者が見る、黙らせた指摘の一覧。"""
    latest: dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT * FROM finding_responses ORDER BY id"):
        latest[row["fingerprint"]] = row
    rows = []
    for row in latest.values():
        if row["kind"] == "off_target":
            data = json.loads(row["finding"])
            rows.append({"user_id": row["user_id"], "rule_id": data["rule_id"], "period": data["period"], "message": data["message"], "who": row["who"], "at": row["at"]})
    return rows


def silenced_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in silenced_list(conn):
        if row["user_id"]:
            counts[row["user_id"]] = counts.get(row["user_id"], 0) + 1
    return counts


def silenced_for_prompt(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """Claude に渡す「人が確認して問題なしとしたもの」。同じ指摘を繰り返さないため。"""
    return [{"rule_id": r["rule_id"], "period": r["period"], "内容": r["message"]} for r in silenced_list(conn) if r["user_id"] == user_id]
