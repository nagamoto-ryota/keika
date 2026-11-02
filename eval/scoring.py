"""採点。当たりは「利用者＋根拠箇所＋理由」がすべて合う時だけ。

書類の中の頁は見ない。標準様式への置き直しで頁が動いたため（書類と日付と理由で照合する）。
項目番号だけでは照合しない。理由は語の一致を機械で見て、言い換えの可能性があるものは
reason_review に出し、別の係が判定する（機械で落とさない）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from keika.findings import Finding


@dataclass
class Score:
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    reason_review: list[tuple[str, str]] = field(default_factory=list)  # 場所は合うが語が無い（言い換えかも）
    nudge_hits: list[str] = field(default_factory=list)
    nudge_misses: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)  # 罠で指摘した
    silent_ok: list[str] = field(default_factory=list)
    silent_review: list[str] = field(default_factory=list)  # ok_if に当たるかもしれないので人が見る
    unknown_item_correct: list[str] = field(default_factory=list)
    unknown_item_errors: list[str] = field(default_factory=list)  # 「分からない」が正解の項目で断定した
    unknowns: list[Finding] = field(default_factory=list)  # 当たりにも誤りにも入れない
    unlisted: list[Finding] = field(default_factory=list)  # 表に無い指摘。答えを見ない係が判定する
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    per_user: dict[str, int] = field(default_factory=dict)


def load_judge_table(path: Path) -> list[dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["items"]


def _users(item: dict) -> list:
    value = item.get("利用者ID")
    return value if isinstance(value, list) else [value]


def _norm_date(value) -> str | None:
    return value.isoformat() if isinstance(value, date) else (str(value) if value else None)


def _same_place(want: dict, finding: Finding) -> bool:
    for ev in finding.evidence:
        if ev.file != want["file"]:
            continue
        if want.get("date") is not None and ev.record_date != _norm_date(want["date"]):
            continue
        return True
    return False


def _location_ok(item: dict, finding: Finding, with_inbox: bool) -> bool:
    wanted = [e for e in item.get("evidence", []) if e.get("role") == "primary"]
    if with_inbox:
        wanted = wanted + list(item.get("evidence_after_inbox") or [])
    if any(_same_place(w, finding) for w in wanted):
        return True
    if finding.searched_scope:  # 「無い」の指摘は、探した範囲が根拠の書類のフォルダを含むこと
        return any(str(Path(w["file"]).parent.as_posix()) in finding.searched_scope for w in wanted)
    return False


def _reason_ok(item: dict, finding: Finding) -> bool:
    names = [Path(e.file).name for e in finding.evidence]
    text = " ".join(filter(None, [finding.message, finding.suggestion, *(e.quote for e in finding.evidence), *names]))
    return all(any(str(_norm_date(w) if isinstance(w, date) else w) in text for w in g["any_of"]) for g in item.get("must_mention", []))


def score(findings: list[Finding], table: list[dict], with_inbox: bool = False) -> Score:
    s = Score()
    used: set[int] = set()
    items = [i for i in table if i["区分"] != "intake" and (with_inbox or not i.get("needs_inbox"))]

    for f in findings:
        if f.user_id:
            s.per_user[f.user_id] = s.per_user.get(f.user_id, 0) + 1
        if f.kind == "unknown":
            s.unknowns.append(f)

    def candidates(item: dict) -> list[Finding]:
        return [f for f in findings if f.user_id in _users(item) and _location_ok(item, f, with_inbox)]

    for item in items:
        kubun, cands = item["区分"], candidates(item)
        if kubun == "finding":
            matched = [f for f in cands if f.kind == "finding"]
            good = [f for f in matched if _reason_ok(item, f)]
            bucket = s.by_kind.setdefault(item.get("judge_kind", "claude"), {"hit": 0, "total": 0})
            bucket["total"] += 1
            if good:
                s.hits.append(item["id"])
                bucket["hit"] += 1
            else:
                s.misses.append(item["id"])
                s.reason_review += [(item["id"], f.message) for f in matched]
            used.update(id(f) for f in matched)
        elif kubun == "nudge":
            good = [f for f in cands if f.kind == "nudge" and _reason_ok(item, f)]
            (s.nudge_hits if good else s.nudge_misses).append(item["id"])
            used.update(id(f) for f in good)
        elif kubun == "silent":
            flagged = [f for f in cands if f.kind == "finding" and _reason_ok(item, f)]
            if not flagged:
                s.silent_ok.append(item["id"])
            elif item.get("ok_if"):
                s.silent_review.append(item["id"])
            else:
                s.false_positives.append(item["id"])
            used.update(id(f) for f in flagged)
        elif kubun == "unknown":
            if any(f.kind == "unknown" for f in cands):
                s.unknown_item_correct.append(item["id"])
            if any(f.kind == "finding" for f in cands):
                s.unknown_item_errors.append(item["id"])
            used.update(id(f) for f in cands)

    s.unlisted = [f for f in findings if id(f) not in used and f.kind != "unknown"]
    return s
