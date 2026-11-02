from datetime import date

import pytest
from fastapi.testclient import TestClient

from keika.claude_checks import CheckRun, ProgressMark, save_run
from keika.db import connect
from keika.files import scan
from keika.findings import Evidence, Finding
from keika.ledger import User, upsert_user
from keika.reading import DocFacts, Source, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.tasks import VISIT_NOTICE_DAYS, build_board
from keika.web.app import create_app

F = "利用者/U01_杉森ハル"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / F).mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    upsert_user(c, User(id="U01", name="杉森 ハル", cm="CM01", data={"type": "居宅介護支援", "cert_period": ["2026-04-01", "2028-03-31"]}))
    upsert_user(c, User(id="U02", name="古賀 正蔵", cm="CM02", data={"type": "居宅介護支援", "cert_period": ["2026-04-01", "2028-03-31"]}))
    yield s, c
    c.close()


def put(s, c, name, facts):
    (s.drive_root / F / name).write_text(name, encoding="utf-8")
    _save(c, next(r for r in scan(s) if r.rel_path == f"{F}/{name}"), facts)


def claude_finding(rule="R07", todo="主治医への交付を確かめる", **kw):
    data = dict(rule_id=rule, user_id="U01", kind="finding", period="2026-08", source="claude", message="交付の記録が見当たりません。確認してください。",
                evidence=[Evidence(file="x.csv", quote="交付")], todo=todo)
    return Finding(**(data | kw))


def tasks_of(board, user_id):
    return next(p for p in board.people if p.user.id == user_id).tasks


def test_task_title_comes_from_finding_todo(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[claude_finding()]))
    assert [t.title for t in tasks_of(build_board(c, s), "U01")] == ["主治医への交付を確かめる"]


def test_finding_without_todo_falls_back_to_rule_title(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[claude_finding(todo=None)]))
    assert tasks_of(build_board(c, s), "U01")[0].title == "プランの交付と交付先の記録"


def test_progress_todo_without_finding_becomes_a_task(env):
    s, c = env
    save_run(c, "U01", CheckRun(progress=[ProgressMark("service_plans", "todo", "", "個別計画の依頼が見当たらない", "", "")]))
    assert [t.title for t in tasks_of(build_board(c, s), "U01")] == ["個別計画の依頼が見当たらない"]


def test_same_thing_is_not_listed_twice(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[claude_finding()], progress=[ProgressMark("delivery", "todo", "", "主治医への交付が見当たらない", "", "")]))
    assert len(tasks_of(build_board(c, s), "U01")) == 1


def test_done_work_is_not_listed(env):
    s, c = env
    save_run(c, "U01", CheckRun(progress=[ProgressMark("delivery", "done", "2026-08-03", "", "", "")]))
    assert tasks_of(build_board(c, s), "U01") == []


def test_person_with_nothing_goes_to_quiet_list(env):
    s, c = env
    board = build_board(c, s)
    assert {p.user.id for p in board.people if not p.tasks} == {"U01", "U02"}
    assert board.total == 0


def test_visit_early_in_month_is_one_line_not_per_person(env):
    s, c = env
    add_record(c, RecordIn(user_id="U01", event_date="2026-10-20", kind_read="居宅訪問", body="訪問。"))
    add_record(c, RecordIn(user_id="U02", event_date="2026-11-02", kind_read="居宅訪問", body="訪問。"))
    board = build_board(c, s)
    assert [u.id for u in board.visit_waiting] == ["U01"] and board.days_left == 26
    assert tasks_of(board, "U01") == []


def test_visit_near_month_end_becomes_a_task(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 30 - VISIT_NOTICE_DAYS + 1))
    c = connect(s)
    upsert_user(c, User(id="U01", name="杉森 ハル", data={"type": "居宅介護支援"}))
    add_record(c, RecordIn(user_id="U01", event_date="2026-10-20", kind_read="居宅訪問", body="訪問。"))
    board = build_board(c, s)
    assert board.visit_waiting == [] and "訪問" in tasks_of(board, "U01")[0].title
    c.close()


def test_unknown_findings_are_folded_separately(env):
    s, c = env
    put(s, c, "票.txt", DocFacts(doc_type="利用票", target_month="2026-10", signature_present=None, signature_source=Source(quote="欄")))
    board = build_board(c, s)
    assert not any("署名" in x.title for x in tasks_of(board, "U01"))
    assert any("署名" in item.finding.message for _, item in board.unknowns)


def test_filter_by_person_in_charge(env):
    s, c = env
    assert [p.user.id for p in build_board(c, s, cm="CM02").people] == ["U02"]


def test_deadline_tasks_come_first(env):
    s, c = env
    upsert_user(c, User(id="U01", name="杉森 ハル", data={"type": "居宅介護支援", "cert_period": ["2025-12-01", "2026-11-30"]}))
    save_run(c, "U01", CheckRun(findings=[claude_finding()]))
    tasks = tasks_of(build_board(c, s), "U01")
    assert tasks[0].deadline == "2026-11-30" and tasks[1].deadline is None


def test_people_with_most_to_do_come_first(env):
    s, c = env
    save_run(c, "U02", CheckRun(findings=[claude_finding(user_id="U02")]))
    assert build_board(c, s).people[0].user.id == "U02"


def test_home_shows_todo_lines_not_tables(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[claude_finding()]))
    body = TestClient(create_app(s)).get("/").text
    assert "やること" in body and "主治医への交付を確かめる" in body
    assert "<table" not in body


def test_home_task_opens_details_with_buttons(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[claude_finding()]))
    body = TestClient(create_app(s)).get("/").text
    assert "交付の記録が見当たりません" in body and "確認した・問題なし" in body


def test_home_has_no_dates_of_past_work(env):
    s, c = env
    put(s, c, "計画.txt", DocFacts(doc_type="居宅サービス計画書", period_start="2026-08-01", period_end="2027-07-31", consent_date="2026-07-30", consent_source=Source(quote="同意")))
    body = TestClient(create_app(s)).get("/").text
    assert "7/30" not in body


def test_press_problem_free_on_home_returns_to_home(env):
    s, c = env
    from keika.findings import fingerprint
    one = claude_finding()
    save_run(c, "U01", CheckRun(findings=[one]))
    client = TestClient(create_app(s))
    r = client.post("/users/U01/respond", data={"fp": fingerprint(one), "kind": "off_target", "back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert "主治医への交付を確かめる" not in client.get("/").text
    r = client.post("/users/U01/respond", data={"fp": fingerprint(one), "kind": "off_target", "back": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/users/U01"


def test_board_page_keeps_the_table_with_marks(env):
    s, c = env
    save_run(c, "U01", CheckRun(findings=[claude_finding()]))
    body = TestClient(create_app(s)).get("/board").text
    for label in ("アセス", "会議録", "交付", "今月の訪問"):
        assert label in body
