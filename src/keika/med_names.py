"""薬名の確認。話のとおり保存し、聞こえた言葉と、書類や確定済みの記録の記載を並べる。

候補はその利用者の書類と確定済みの記録にある語の文字の近さだけで探す。Claude には候補を出させない。
直すのは人が押した時だけで、押した結果は消せない形で残す。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePosixPath

from keika.claude_checks import _ref_text
from keika.ledger import get_user, list_users
from keika.machine_checks import user_refs
from keika.records import Record, edit_record, get_record, list_records
from keika.settings import Settings, now_real

_KATAKANA = re.compile(r"[ァ-ヶー]{3,}")
_MEDICINE_SENTENCE = re.compile(r"薬|錠|服|処方|mg|ミリ|カプセル|貼付|軟膏|点眼|インスリン")
NEAR = 0.6  # これより文字が近い語だけを並べる
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class Candidate:
    name: str
    where: str
    doc_path: str | None = None


@dataclass
class MedCheck:
    id: int
    record: Record
    user_name: str
    heard: str
    candidates: list[Candidate] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    heard: str
    status: str  # open / fixed / kept / edited
    chosen: str
    editor: str
    decided_at: str


def _katakana(text: str) -> str:
    return re.sub(r"[ぁ-ゖ]", lambda m: chr(ord(m[0]) + 0x60), text)


def _doc_label(rel_path: str) -> str:
    return re.split(r"[_ ]\d{4}", PurePosixPath(rel_path).stem)[0]


def _sources(conn: sqlite3.Connection, s: Settings, user_id: str) -> list[tuple[str, str | None, str]]:
    """(どこ, 書類のパス, 本文)。確定済みの記録だけを入れ、未確定の記録と薬名が未確認の記録は入れない。"""
    out: list[tuple[str, str | None, str]] = []
    for ref in user_refs(conn, s, get_user(conn, user_id)):
        text = _ref_text(s, ref)
        if text:
            out.append((_doc_label(ref.rel_path), ref.rel_path, text))
    waiting = {r["record_id"] for r in conn.execute("SELECT record_id FROM med_checks WHERE status = 'open'")}
    for r in list_records(conn, user_id):
        if not r.unchecked and r.id not in waiting:
            month, day = r.event_date.split("-")[1:]
            out.append((f"{int(month)}/{int(day)}の確定済みの記録", None, r.body))
    return out


def candidates(conn: sqlite3.Connection, s: Settings, user_id: str, heard: str) -> list[Candidate]:
    heard = _katakana(heard)
    scored: dict[str, tuple[float, Candidate]] = {}
    for where, path, text in _sources(conn, s, user_id):
        for word in set(_KATAKANA.findall(text)):
            score = SequenceMatcher(None, heard, word).ratio()
            if word != heard and score >= NEAR and (word not in scored or score > scored[word][0]):
                scored[word] = (score, Candidate(word, where, path))
    best = sorted(scored.values(), key=lambda x: -x[0])[:MAX_CANDIDATES]
    return [c for _, c in best]


def _written_exactly(conn: sqlite3.Connection, s: Settings, user_id: str, heard: str) -> bool:
    return any(_katakana(heard) in text for _, _, text in _sources(conn, s, user_id))


def register_checks(conn: sqlite3.Connection, s: Settings, record: Record, unsure_words: list[str]) -> None:
    """Claude が迷った語と、薬の文の中で書類の語に近い語を、確認の対象にする。保存は止めない。"""
    words = [w for w in unsure_words if w in record.body]
    for sentence in re.split(r"[。\n]", record.body):
        if _MEDICINE_SENTENCE.search(sentence):
            words += [w for w in _KATAKANA.findall(sentence) if candidates(conn, s, record.user_id, w)]
    with conn:
        for word in dict.fromkeys(words):
            if not _written_exactly(conn, s, record.user_id, word):
                conn.execute("INSERT INTO med_checks (record_id, heard, status, created_at) VALUES (?, ?, 'open', ?)", (record.id, word, now_real().isoformat()))


def open_checks(conn: sqlite3.Connection, s: Settings, cm: str | None = None) -> list[MedCheck]:
    names = {u.id: u.name for u in list_users(conn, cm=cm)}
    checks = []
    for row in conn.execute("SELECT * FROM med_checks WHERE status = 'open' ORDER BY id").fetchall():
        record = get_record(conn, row["record_id"])
        if row["heard"] not in record.body:  # 人が本文を直して語が無くなった
            with conn:
                conn.execute("UPDATE med_checks SET status = 'edited', decided_at = ? WHERE id = ?", (now_real().isoformat(), row["id"]))
            continue
        if record.user_id in names:
            checks.append(MedCheck(row["id"], record, names[record.user_id], row["heard"], candidates(conn, s, record.user_id, row["heard"])))
    return checks


def unconfirmed_record_ids(conn: sqlite3.Connection, s: Settings) -> set[int]:
    return {c.record.id for c in open_checks(conn, s)}


def message(check: MedCheck) -> list[str]:
    """聞こえた言葉と記載を並べるだけ。どちらが正しいかは言わない。"""
    lines = [f"「{check.heard}」と聞こえました。"]
    if not check.candidates:
        return lines + ["薬の名前を確かめてください。"]
    return lines + [f"{c.where}には「{c.name}」とあります。" for c in check.candidates]


def _open_row(conn: sqlite3.Connection, check_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM med_checks WHERE id = ? AND status = 'open'", (check_id,)).fetchone()
    if row is None:
        raise ValueError("確認待ちの薬名が見つかりません")
    return row


def _close(conn: sqlite3.Connection, check_id: int, status: str, chosen: str, editor: str) -> None:
    with conn:
        conn.execute("UPDATE med_checks SET status = ?, chosen = ?, editor = ?, decided_at = ? WHERE id = ?", (status, chosen, editor, now_real().isoformat(), check_id))


def decide_fix(conn: sqlite3.Connection, s: Settings, check_id: int, chosen: str, editor: str) -> Record:
    row = _open_row(conn, check_id)
    record = get_record(conn, row["record_id"])
    if chosen not in [c.name for c in candidates(conn, s, record.user_id, row["heard"])]:
        raise ValueError("書類や確定済みの記録に無い名前には直せません")
    fixed = edit_record(conn, record.id, {"body": record.body.replace(row["heard"], chosen)}, editor=editor or "画面")
    _close(conn, check_id, "fixed", chosen, editor)
    return fixed


def decide_keep(conn: sqlite3.Connection, check_id: int, editor: str) -> None:
    _open_row(conn, check_id)
    _close(conn, check_id, "kept", "", editor)


def decisions(conn: sqlite3.Connection, record_id: int) -> list[Decision]:
    rows = conn.execute("SELECT * FROM med_checks WHERE record_id = ? AND status != 'open' ORDER BY id", (record_id,))
    return [Decision(r["heard"], r["status"], r["chosen"] or "", r["editor"] or "", r["decided_at"]) for r in rows]
