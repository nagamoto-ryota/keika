"""開くと終わっている入口。上限、前回開いた後、期限の一言。項目の数は変えない。"""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import keika.web
from conftest import SAMPLE
from keika.claude_checks import CheckRun, save_run
from keika.db import connect
from keika.draft_card import draft_view
from keika.drafts import WireDraft, WireItem, WireSection, get_draft, make_draft
from keika.findings import Evidence, Finding
from keika.home import DAILY_ASKS, deadline_lines, new_records, opened_since, pick_asks
from keika.intake import to_review
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.secretary import start_question
from keika.settings import Settings, now_real
from keika.tasks import build_board
from keika.web.app import create_app
from test_reading import FakeClient, answer


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def home(s):
    return TestClient(create_app(s)).get("/").text


def spoken(c, user="U04"):
    return add_record(c, RecordIn(user_id=user, event_date="2026-11-05", kind_read="電話", body="話した。", source="intake:話.txt"))


def card(c, n=0):
    entry = {"user_id": "U02", "spoken_name": "古河さん", "ambiguous": False, "ambiguity_note": "", "event_date": "2026-11-04", "event_time": "", "kind_read": "電話", "monitoring": False, "partner": "本人", "body": f"話した{n}。", "name_fixes": [], "unsure_words": []}
    to_review(c, f"話{n}", "typed", entry, [], "「古河さん」が誰か確かめてください")


def draft(s, c, text="デイは週3回のまま続ける。"):
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="居宅訪問", monitoring=True, body="話した。"))
    wire = WireDraft(place="", start_time="", end_time="", attendees=[], sections=[WireSection(key="services", items=[WireItem(text=text, quote="デイは週2回のまま続けます")])])
    return make_draft(c, FakeClient(answer(wire)), s, r, "デイは週2回のまま続けます。", "話.txt")


def test_says_what_was_recorded_since_last_open(env):
    s, c = env
    spoken(c)
    html = home(s)
    assert "記録しました <b>1</b>件" in html and "柚木 茂" in html  # 11/6 入口を今日の結果＋あなたの番に


def test_csv_records_are_not_counted(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="電話", body="取り込み。", source="csv:利用者/x.csv"))
    assert "新しい記録はありません" in home(s)


def test_reloading_keeps_the_list_and_a_later_visit_clears_it(env, monkeypatch):
    s, c = env
    spoken(c)
    assert len(new_records(c, opened_since(c))) == 1
    assert len(new_records(c, opened_since(c))) == 1  # 続けて開いても消えない
    later = now_real() + timedelta(hours=2)
    monkeypatch.setattr("keika.home.now_real", lambda: later)
    assert new_records(c, opened_since(c)) == []


def test_daily_limit_and_the_rest_is_counted(env):
    s, c = env
    keys = [f"r{i}" for i in range(DAILY_ASKS + 3)]
    picked, more = pick_asks(c, s, keys)
    assert picked == keys[:DAILY_ASKS] and more == 3


def test_answered_ones_still_count_toward_today(env):
    s, c = env
    keys = [f"r{i}" for i in range(DAILY_ASKS + 3)]
    pick_asks(c, s, keys)
    picked, more = pick_asks(c, s, keys[2:])  # 2件答えた
    assert picked == keys[2:DAILY_ASKS] and more == 3  # 新しい物は足さない


def test_next_day_opens_new_room(env):
    s, c = env
    keys = [f"r{i}" for i in range(DAILY_ASKS + 3)]
    pick_asks(c, s, keys)
    tomorrow = Settings(drive_root=s.drive_root, db_path=s.db_path, as_of=date(2026, 11, 6))
    picked, more = pick_asks(c, tomorrow, keys[DAILY_ASKS:])
    assert picked == keys[DAILY_ASKS:] and more == 0


def test_page_shows_at_most_the_limit_and_says_how_many_more(env):
    s, c = env
    for n in range(DAILY_ASKS + 2):
        card(c, n)
    html = home(s)
    assert html.count('data-compare="/review/') == DAILY_ASKS and "あと 2件" in html


def test_review_card_is_answerable_on_the_page(env):
    s, c = env
    card(c)
    html = home(s)
    assert ">いいよ</button>" in html and "<summary>違う</summary>" in html


def test_draft_with_checks_is_one_row_to_answer(env):
    s, c = env
    i = draft(s, c)
    assert f"/drafts/{i}#kakunin" in home(s)


def test_blank_fields_alone_are_not_asked(env):
    s, c = env
    i = draft(s, c, text="デイは週2回のまま続ける。")
    assert draft_view(c, get_draft(c, i))["checks"] == []
    assert f"/drafts/{i}#kakunin" not in home(s)


def test_findings_are_not_on_the_daily_page(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[Finding(rule_id="R07", user_id="U04", kind="finding", period="2026-10", source="claude", message="交付の記録が見当たりません。", evidence=[Evidence(file="x.csv", quote="計画")], todo="主治医への交付を確かめる")]))
    http = TestClient(create_app(s))
    assert "交付の記録が見当たりません" not in http.get("/").text
    assert "交付の記録が見当たりません" in http.get("/todo").text
    assert "交付の記録が見当たりません" in http.get("/users/U04").text


def test_near_deadline_gets_one_line_from_the_secretary(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[Finding(rule_id="R45", user_id="U04", kind="nudge", period="2026-11-30", source="machine", message="計画の期間が終わります。", evidence=[], searched_scope="計画", todo="計画の見直しの準備")]))
    lines = deadline_lines(build_board(c, s), s)
    assert lines[0]["days"] == 25 and lines[0]["user_id"] == "U04"
    html = home(s)
    assert "計画の見直しの準備" in html  # 「ケイカより」の前置きは外した（11/6） and "あと25日" in html and "/desk?t=U04" in html


def test_far_deadline_stays_off(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[Finding(rule_id="R45", user_id="U04", kind="nudge", period="2027-03-31", source="machine", message="計画の期間が終わります。", evidence=[], searched_scope="計画", todo="計画の見直しの準備")]))
    assert "U04" not in [x["user_id"] for x in deadline_lines(build_board(c, s), s)]


def test_unchecked_count_and_total_are_small_but_there(env):
    s, c = env
    spoken(c)
    card(c)
    html = home(s)
    assert "未確認の記録 1件" in html and "最後に決めるのは人" in html and "/todo" in html
    todo = TestClient(create_app(s)).get("/todo").text
    decide = int(re.search(r'今決めること <span class="count">(\d+)', todo)[1])
    others = int(re.search(r"ほかに (\d+)件", todo)[1])
    assert f"すべての項目 {decide + others}件" in html  # 合計は変えない


def test_opening_the_home_page_in_sample_mode_writes_nothing(env):
    s, c = env
    r = TestClient(create_app(s, sample=True)).get("/")
    assert r.status_code == 200
    assert c.execute("SELECT COUNT(*) FROM home_asks").fetchone()[0] == 0


def test_blanks_are_shown_in_the_body_and_saving_is_open(env):
    s, c = env
    i = draft(s, c, text="デイは週2回のまま続ける。")
    html = TestClient(create_app(s)).get(f"/drafts/{i}").text
    assert "赤い所を埋めてください" in html and "残っていても保存できます" in html and 'name="end_time"' in html


def test_draft_with_checks_shows_blanks_too(env):
    s, c = env
    i = draft(s, c)
    assert "赤と黄色の所を直してください" in TestClient(create_app(s)).get(f"/drafts/{i}").text


# --- F2 の残り ---


def test_at_most_three_answers_at_once(env):
    _, c = env
    for user in ("U01", "U02", "U03"):
        start_question(c, user, "質問")
    with pytest.raises(ValueError):
        start_question(c, "U04", "4人目")


def test_no_screen_calls_it_by_the_old_name(env):
    """呼び名は「AI相談」「ケイカに聞く」。コード内の名前と URL は変えない。"""
    web = Path(keika.web.__file__).parent
    old = "秘" + "書"
    for path in [*(web / "templates").iterdir(), *(web / "static").iterdir()]:
        if path.is_file():
            assert old not in path.read_text(encoding="utf-8"), path.name
    assert ">AI相談</a>" in home(env[0])
