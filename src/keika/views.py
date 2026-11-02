"""画面に出す形。ケアマネが見るべきものだけを短く出し、原文は押した時に開く。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote as url_quote

from keika.files import DocRef, to_payload
from keika.findings import Evidence
from keika.records import Record
from keika.settings import OutsideRootError, Settings, safe_path

_FULL = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MONTH = re.compile(r"(\d{4})-(\d{2})(?!\d)")
_TAIL = re.compile(r"(_\d{4}-\d{2}(-\d{2})?)+(_.*)?$")
_SPACE = re.compile(r"\s+")


def hdate(text: str | None, year: int) -> str:
    """2026-08-05 → 8/5、2026-09 → 9月。別の年の時だけ年を残す。"""
    if not text:
        return ""

    def full(m: re.Match) -> str:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return f"{mo}/{d}" if y == year else f"{y}/{mo}/{d}"

    def month(m: re.Match) -> str:
        y, mo = int(m[1]), int(m[2])
        return f"{mo}月" if y == year else f"{y}年{mo}月"

    return _MONTH.sub(month, _FULL.sub(full, text))


@dataclass
class EvidenceView:
    label: str
    context: str
    highlight: str | None
    open_url: str | None
    unverified: bool = False


def _doc_label(file: str) -> str:
    stem = PurePosixPath(file).stem
    return _TAIL.sub("", stem) or stem


def _around(text: str, quote: str | None) -> str | None:
    """引用を含む行と前後1行を返す。"""
    if not quote:
        return None
    needle = _SPACE.sub("", quote)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if needle in _SPACE.sub("", line):
            return "\n".join(lines[max(0, i - 1):i + 2])
    return None


def evidence_view(s: Settings, e: Evidence, records: list[Record], year: int) -> EvidenceView:
    highlight = e.quote or None
    if e.record_date:
        rows = [r for r in records if r.event_date == e.record_date]
        match = [r for r in rows if e.quote and _SPACE.sub("", e.quote) in _SPACE.sub("", r.body)] or rows
        context = "\n\n".join(r.body for r in match) or (e.quote or "")
        return EvidenceView(f"支援経過 {hdate(e.record_date, year)}", context, highlight, None)
    label = _doc_label(e.file) + (f" {e.page}ページ" if e.page else "")
    try:
        path = safe_path(s, e.file)
    except OutsideRootError:
        return EvidenceView(label, e.quote or "", highlight, None)
    if not path.is_file():
        return EvidenceView(label, e.quote or "", highlight, None)
    url = f"/doc?path={url_quote(e.file)}"
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        return EvidenceView(label, e.quote or "", highlight, url + (f"#page={e.page}" if e.page else ""), e.unverified)
    payload = to_payload(s, DocRef(e.file, "", ext, "user", None))
    context = _around(payload.text, e.quote) if payload.text else None
    return EvidenceView(label, context or e.quote or "", highlight, url)
