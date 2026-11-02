"""Finding の型。機械の点検も Claude の点検も、結果はこの形で画面に出す。"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel


class Evidence(BaseModel):
    file: str  # 読み取りの根からの相対パス
    page: int | None = None
    quote: str | None = None
    record_date: str | None = None


class Finding(BaseModel):
    rule_id: str
    user_id: str | None  # None は事業所の宿題
    kind: Literal["nudge", "finding", "unknown"]
    period: str = ""  # 対象の月など
    message: str
    todo: str | None = None  # 一覧に出す「何をするか」の短い一行
    evidence: list[Evidence] = []
    searched_scope: str | None = None  # 「無い」時は、探した範囲を箇所の代わりに示す
    suggestion: str | None = None
    source: Literal["machine", "claude"] = "machine"


def fingerprint(f: Finding) -> str:
    """同じ指摘かどうかの目印。言い回しは Claude の回ごとに変わるので入れない。"""
    places = sorted({(e.file, str(e.page or ""), e.record_date or "") for e in f.evidence})
    raw = "\0".join([f.rule_id, f.user_id or "", f.period, f.kind, str(places)])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
