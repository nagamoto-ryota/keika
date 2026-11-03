"""画面の整理: やることは1か所、工程の表で矛盾を出さない、警告は初見で分かる言葉で。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from keika.claude_checks import CheckRun, ProgressMark, save_run
from keika.db import connect
from keika.files import scan
from keika.intake import open_reviews, term_context, to_review
from keika.ledger import User, get_user, upsert_user
from keika.progress import user_progress
from keika.reading import DocFacts, _save
from keika.settings import Settings
from keika.web.app import create_app

F = "利用者/U01_杉森ハル"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / F).mkdir(parents=True)
    (root / "事業所").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    upsert_user(c, User(id="U01", name="杉森 ハル", cm="CM01", data={"type": "居宅介護支援", "cert_period": ["2026-04-01", "2028-03-31"]}))
    yield s, c
    c.close()


def put(s, c, name, facts):
    (s.drive_root / F / name).write_text(name, encoding="utf-8")
    _save(c, next(r for r in scan(s) if r.rel_path == f"{F}/{name}"), facts)


def plan_doc():
    return DocFacts(doc_type="居宅サービス計画書", period_start="2026-08-01", period_end="2027-07-31", doc_date="2026-07-30", consent_date="2026-07-30")


def mark(c, status):
    run = CheckRun(progress=[ProgressMark("meeting", status, "", "ほたるいの照会の中身がありません", "", "")])
    save_run(c, "U01", run)


def cell(s, c, key):
    return next(x for x in user_progress(c, s, get_user(c, "U01")) if x.key == key)


def test_meeting_is_not_yet_only_when_there_are_no_minutes(env):
    s, c = env
    put(s, c, "計画.txt", plan_doc())
    put(s, c, "要点.txt", DocFacts(doc_type="サービス担当者会議の要点", doc_date="2026-07-30"))
    mark(c, "todo")
    x = cell(s, c, "meeting")
    assert x.status == "check" and "ほたるい" in x.note


def test_meeting_stays_todo_without_minutes(env):
    s, c = env
    put(s, c, "計画.txt", plan_doc())
    mark(c, "todo")
    assert cell(s, c, "meeting").status == "todo"


def test_consent_after_start_is_check_not_missing(env):
    s, c = env
    d = plan_doc().model_copy(update={"consent_date": "2026-08-10"})
    put(s, c, "計画.txt", d)
    assert cell(s, c, "consent").status == "check"


def test_board_shows_check_word(env):
    s, c = env
    put(s, c, "計画.txt", plan_doc())
    put(s, c, "要点.txt", DocFacts(doc_type="サービス担当者会議の要点", doc_date="2026-07-30"))
    mark(c, "todo")
    assert ">確認<" in TestClient(create_app(s)).get("/board").text


def test_check_cell_becomes_a_task(env):
    s, c = env
    put(s, c, "計画.txt", plan_doc())
    put(s, c, "要点.txt", DocFacts(doc_type="サービス担当者会議の要点", doc_date="2026-07-30"))
    mark(c, "todo")
    assert "ほたるいの照会の中身がありません" in TestClient(create_app(s)).get("/").text


def test_nav_has_three_entries(env):
    s, c = env
    body = TestClient(create_app(s)).get("/").text
    nav = body[body.index("<nav>"):body.index("</nav>")]
    assert nav.count("<a ") == 3 and "記録する" in nav and "やること" in nav and "一覧" in nav


def test_list_pages_share_tabs(env):
    s, c = env
    client = TestClient(create_app(s))
    for path in ("/board", "/office", "/ledger", "/silenced"):
        body = client.get(path).text
        assert all(href in body for href in ('href="/board"', 'href="/office"', 'href="/ledger"', 'href="/silenced"')), path


def test_office_tasks_and_reviews_appear_on_the_one_todo_page(env):
    s, c = env
    to_review(c, "長女から電話。", "typed", {"user_id": "U01", "body": "長女から電話があった。"}, [], "「長女」が誰か確かめてください")
    body = TestClient(create_app(s)).get("/").text
    assert "長女から電話があった。" in body  # 記録の確認がその場で処理できる
    assert 'action="/office/' in body  # 事業所のことも同じページ


def test_review_page_redirects_to_todo(env):
    s, c = env
    assert TestClient(create_app(s)).get("/review", follow_redirects=False).status_code == 303


def test_saving_a_review_returns_to_todo(env):
    s, c = env
    rid = to_review(c, "長女から電話。", "typed", {"user_id": "U01", "event_date": "2026-11-02", "event_time": "", "kind_read": "電話", "monitoring": False, "partner": "", "body": "長女から電話があった。"}, [], "")
    r = TestClient(create_app(s)).post(f"/review/{rid}", data={"user_id": "U01", "body": "x", "action": "save"}, follow_redirects=False)
    assert r.headers["location"].split("#")[0] == "/"


def test_cm_tab_hides_office_and_office_tab_exists(env):
    s, c = env
    client = TestClient(create_app(s))
    assert 'action="/office/' not in client.get("/?cm=CM01").text
    assert 'action="/office/' in client.get("/?cm=office").text


def test_term_context_shows_words_around_the_number():
    assert term_context("本人宅へ14時から30分程度訪問した。", "14") == "本人宅へ14時から30分程度訪問した"
    assert term_context("朝は元気。血圧は120だった。", "120") == "血圧は120だった"
    assert term_context("なし", "99") == "99"


def test_warning_is_shown_in_plain_words(env):
    s, c = env
    to_review(c, "訪問した", "typed", {"user_id": "U01", "body": "15時から訪問した。"}, ["15"], "")
    body = TestClient(create_app(s)).get("/").text
    assert "話の中にありません" in body and "15時から訪問した" in body
    assert "話に無いかもしれない語" not in body
    assert open_reviews(c)[0]["hints"][0]["context"] == "15時から訪問した"
