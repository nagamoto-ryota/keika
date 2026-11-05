"""やること一覧。やるべきことのうち、やっていないものだけを、何をするかの一行で出す。

済んだ工程は出さない。同じことを指摘と進捗で二重に出さない。
月の訪問は、まだ時間がある間は人ごとに出さず一行にまとめる。
"""
from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field

from keika.drafts import Homework, open_homework
from keika.findings import Finding
from keika.ledger import User, list_users
from keika.progress import Cell, user_progress
from keika.responses import Item, sort_findings
from keika.rules import get_rule
from keika.settings import Settings, today

VISIT_NOTICE_DAYS = 10  # 月末までこの日数を切ったら人ごとに出す
DEADLINE_RULES = ("R44", "R45")
UPDATE_RULES = ("R44", "R45", "R09")
STEP_RULE = {
    "assessment": "R09", "meeting": "R01", "minutes": "R33", "consent": "R10", "delivery": "R07",
    "service_plans": "R12", "medical": "R08", "visit": "R06", "usage": "R13",
}


@dataclass
class Task:
    title: str
    item: Item | None = None  # 開いた時に見せる指摘。進捗だけから来たものは無い
    deadline: str | None = None
    note: str = ""
    view: dict | None = None  # 画面に出す形（画面の側で入れる）
    homework: list[Homework] = field(default_factory=list)  # 会議や訪問で残った宿題。人ごとに1行にまとめる

    @property
    def opens(self) -> str:
        """押すと何が開くか。空なら開く物が無く、利用者のページへ直接行く。"""
        if self.homework:
            return "宿題の一覧"
        if self.item:
            return "根拠と書き足す文の案" if self.item.finding.suggestion else "根拠と次の手順"
        return ""


@dataclass
class Person:
    user: User
    tasks: list[Task] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)


@dataclass
class Board:
    people: list[Person]
    visit_waiting: list[User]
    days_left: int
    unknowns: list[tuple[User, Item]]

    @property
    def total(self) -> int:
        return sum(len(p.tasks) for p in self.people)


def _is_month_visit(f: Finding) -> bool:
    return f.rule_id == "R06" and f.kind == "nudge" and "〜" not in f.period and f.todo != "訪問の話を記録にする"


def _person(conn: sqlite3.Connection, s: Settings, user: User, days_left: int, board_visit: list, unknowns: list) -> Person:
    from keika.web.app import current_findings

    found, _ = current_findings(conn, s, user.id)
    person = Person(user, cells=user_progress(conn, s, user))
    covered = set()
    for item in sort_findings(conn, s, user.id, found).open:
        f = item.finding
        covered.add(f.rule_id)
        if f.kind == "unknown":
            unknowns.append((user, item))
        elif _is_month_visit(f) and days_left > VISIT_NOTICE_DAYS:
            board_visit.append(user)
        else:
            deadline = f.period if f.rule_id in DEADLINE_RULES else None
            person.tasks.append(Task(f.todo or get_rule(f.rule_id).title, item, deadline))
    for cell in person.cells:
        if cell.status in ("todo", "check") and cell.key != "visit" and STEP_RULE[cell.key] not in covered:
            person.tasks.append(Task(cell.note or f"{cell.label}がまだです", note=cell.note))
    homework = open_homework(conn, user.id)
    if homework:
        person.tasks.append(Task(f"宿題・持ち越し {len(homework)}件", homework=homework))
    person.tasks.sort(key=lambda t: (t.deadline is None, t.deadline or ""))
    return person


def _is_update(task: Task) -> bool:
    """更新の期限に向けた準備。同じ趣旨が別の点検から重なって出る。"""
    if not task.item:
        return False
    rule = task.item.finding.rule_id
    return rule in UPDATE_RULES or (rule == "R11" and "更新" in task.title)


def person_line(tasks: list[Task]) -> dict:
    """1人を1行に畳む形。表示の上でまとめるだけで、項目は減らさない。"""
    dated = [t for t in tasks if t.deadline]
    lead = min(dated, key=lambda t: t.deadline) if dated else (tasks[0] if tasks else None)
    updates = [t for t in tasks if _is_update(t)]
    rows: list[dict] = []
    grouped = len(updates) >= 2
    for task in tasks:
        if grouped and _is_update(task):
            if task is updates[0]:
                deadlines = [t.deadline for t in updates if t.deadline]
                rows.append({"group": "更新の準備", "tasks": updates, "deadline": min(deadlines) if deadlines else None})
        else:
            rows.append({"task": task})
    return {"count": len(tasks), "lead": lead.title if lead else "", "deadline": lead.deadline if lead else None, "rows": rows}


def person_lines(board: Board) -> list[dict]:
    """期限の近い人から。期限が無ければ件数の多い順（一覧の並びのまま）。"""
    lines = [person_line(p.tasks) | {"user": p.user} for p in board.people if p.tasks]
    return sorted(lines, key=lambda x: (x["deadline"] is None, x["deadline"] or ""))


def build_board(conn: sqlite3.Connection, s: Settings, cm: str | None = None) -> Board:
    on = today(s)
    days_left = calendar.monthrange(on.year, on.month)[1] - on.day
    visit: list[User] = []
    unknowns: list[tuple[User, Item]] = []
    people = [_person(conn, s, u, days_left, visit, unknowns) for u in list_users(conn, cm=cm)]
    people.sort(key=lambda p: -len(p.tasks))
    return Board(people, visit, days_left, unknowns)
