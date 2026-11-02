from datetime import date

import pytest
from fastapi.testclient import TestClient

from keika.claude_checks import CheckRun, ProgressMark, save_run
from keika.db import connect
from keika.files import scan
from keika.ledger import User, get_user, upsert_user
from keika.progress import STEPS, user_progress
from keika.reading import DocFacts, Source, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app

F = "利用者/U01_杉森ハル"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / F).mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    upsert_user(c, User(id="U01", name="杉森 ハル", cm="CM01", data={"type": "居宅介護支援", "cert_period": ["2026-04-01", "2028-03-31"]}))
    yield s, c
    c.close()


def put(s, c, name, facts):
    (s.drive_root / F / name).write_text(name, encoding="utf-8")
    ref = next(r for r in scan(s) if r.rel_path == f"{F}/{name}")
    _save(c, ref, facts)
    return ref.rel_path


def plan(**kw):
    return DocFacts(**(dict(doc_type="居宅サービス計画書", period_start="2026-08-01", period_end="2027-07-31", doc_date="2026-07-30",
                            consent_date="2026-07-30", consent_source=Source(quote="同意")) | kw))


def cell(s, c, key):
    return next(x for x in user_progress(c, s, get_user(c, "U01")) if x.key == key)


def test_all_steps_are_returned_in_order(env):
    s, c = env
    keys = [x.key for x in user_progress(c, s, get_user(c, "U01"))]
    assert keys == [st.key for st in STEPS]


def test_no_plan_read_means_unknown_not_missing(env):
    s, c = env
    assert cell(s, c, "assessment").status == "unknown"


def test_assessment_done_when_dated_near_plan_start(env):
    s, c = env
    put(s, c, "計画.txt", plan())
    rel = put(s, c, "アセス.txt", DocFacts(doc_type="アセスメント", doc_date="2026-07-22"))
    x = cell(s, c, "assessment")
    assert x.status == "done" and x.date == "2026-07-22" and x.file == rel


def test_assessment_todo_when_only_old_one(env):
    s, c = env
    put(s, c, "計画.txt", plan())
    put(s, c, "アセス.txt", DocFacts(doc_type="アセスメント", doc_date="2024-01-10"))
    assert cell(s, c, "assessment").status == "todo"


def test_minutes_done_when_summary_document_exists(env):
    s, c = env
    put(s, c, "計画.txt", plan())
    put(s, c, "要点.txt", DocFacts(doc_type="サービス担当者会議の要点", doc_date="2026-07-30"))
    assert cell(s, c, "minutes").status == "done"


def test_minutes_todo_when_all_read_and_none(env):
    s, c = env
    put(s, c, "計画.txt", plan())
    assert cell(s, c, "minutes").status == "todo"


def test_consent_after_start_is_todo(env):
    s, c = env
    put(s, c, "計画.txt", plan(consent_date="2026-08-05"))
    x = cell(s, c, "consent")
    assert x.status == "todo" and x.date == "2026-08-05"


def test_consent_before_start_is_done(env):
    s, c = env
    put(s, c, "計画.txt", plan())
    assert cell(s, c, "consent").status == "done"


def test_visit_this_month(env):
    s, c = env
    assert cell(s, c, "visit").status == "todo"
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-02", kind_read="居宅訪問", body="訪問。"))
    x = cell(s, c, "visit")
    assert x.status == "done" and x.date == "2026-11-02"


def test_usage_sign_this_month(env):
    s, c = env
    put(s, c, "票.txt", DocFacts(doc_type="利用票", target_month="2026-11", signature_present=True))
    assert cell(s, c, "usage").status == "done"


def test_usage_blank_signature_is_todo_and_unread_is_unknown(env):
    s, c = env
    put(s, c, "票.txt", DocFacts(doc_type="利用票", target_month="2026-11", signature_present=False))
    assert cell(s, c, "usage").status == "todo"
    put(s, c, "票.txt", DocFacts(doc_type="利用票", target_month="2026-11", signature_present=None))
    assert cell(s, c, "usage").status == "unknown"


def test_medical_step_uses_claude_mark_and_na_when_no_medical_service(env):
    s, c = env
    assert cell(s, c, "medical").status == "unknown"  # Claude がまだ読んでいない
    save_run(c, "U01", CheckRun(progress=[ProgressMark("medical", "na", "", "医療系サービスなし", "", "")]))
    assert cell(s, c, "medical").status == "na"


def test_claude_marks_carry_evidence(env):
    s, c = env
    save_run(c, "U01", CheckRun(progress=[ProgressMark("delivery", "done", "2026-08-03", "主治医まで交付", "利用者/a.csv", "FAXで送付")]))
    x = cell(s, c, "delivery")
    assert x.status == "done" and x.date == "2026-08-03" and x.note == "主治医まで交付" and x.quote == "FAXで送付"


def test_failed_run_does_not_erase_last_good_marks(env):
    s, c = env
    save_run(c, "U01", CheckRun(progress=[ProgressMark("delivery", "done", "2026-08-03", "交付", "", "")]))
    save_run(c, "U01", CheckRun(not_run=True, error="通信"))
    assert cell(s, c, "delivery").status == "done"


def test_dashboard_shows_progress_board(env):
    s, c = env
    put(s, c, "計画.txt", plan())
    body = TestClient(create_app(s)).get("/").text
    for label in ("アセス", "会議", "会議録", "同意", "交付", "個別計画", "医療系", "今月の訪問", "利用票"):
        assert label in body
    assert "杉森 ハル" in body

