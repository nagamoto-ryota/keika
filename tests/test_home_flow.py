"""入口を「訪問から帰ったら、もう終わっている」の1本の流れにする（11/6 試用の声）。

一番上に今日の結果を4つの数で出し、その下は「あなたの番」だけを1件1行で並べる。押した時に開く。
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import CheckRun, save_run
from keika.db import connect
from keika.findings import Evidence, Finding
from keika.intake import to_review
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app


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


def card(c):
    entry = {"user_id": "U02", "spoken_name": "古河さん", "ambiguous": False, "ambiguity_note": "", "event_date": "2026-11-04", "event_time": "", "kind_read": "電話",
             "monitoring": False, "partner": "本人", "body": "電話で話した。デイの日を増やしたいとのこと。", "name_fixes": [], "unsure_words": []}
    to_review(c, "古河さんと電話で話した。", "typed", entry, [], "「古河さん」が誰か確かめてください")


def meeting_finding(c):
    save_run(c, "U04", CheckRun(findings=[Finding(rule_id="R01", user_id="U04", kind="finding", period="2026-10", source="claude", message="担当者会議の記録が見当たりません。",
                                                  evidence=[Evidence(file="利用者/U04_柚木茂/利用票.pdf", page=1, quote="月・水・木")])]))


def deadline(c):
    save_run(c, "U05", CheckRun(findings=[Finding(rule_id="R45", user_id="U05", kind="nudge", period="2026-11-30", source="machine", message="計画の期間が終わります。",
                                                  evidence=[], searched_scope="計画", todo="計画の見直しの準備")]))


def test_todays_results_come_first_as_four_numbers(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="電話", body="話した。", source="intake:話.txt"))
    card(c)
    deadline(c)
    html = home(s)
    top = html.split('class="home-summary"')[1].split("</div>")[0]
    for words in ("記録しました", "確かめてほしいこと", "用意したメール", "近い期限"):
        assert words in top
    assert ">1</b>件" in top.split("記録しました")[1][:40]
    assert html.index('class="home-summary"') < html.index("あなたの番")


def test_the_recorded_count_opens_the_list(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="電話", body="話した。", source="intake:話.txt"))
    html = home(s)
    assert 'href="#kiroku"' in html and 'id="kiroku"' in html and "柚木 茂" in html.split('id="kiroku"')[1]


def test_review_card_starts_as_a_short_line_with_two_buttons(env):
    s, c = env
    card(c)
    html = home(s)
    one = html.split('data-compare=')[1].split("</article>")[0]
    assert ">いいよ</button>" in one and "<summary>違う</summary>" in one
    more = one.index("<summary>詳しく見る</summary>")
    assert '<details class="card-more">' in one  # 初めは閉じている
    assert more < one.index("誰の記録か選ぶ") and more < one.index("全文を見て直す")


def test_review_card_on_the_todo_page_is_unchanged(env):
    s, c = env
    card(c)
    html = TestClient(create_app(s)).get("/todo").text
    assert "<summary>詳しく見る</summary>" not in html.split('data-compare=')[1].split("</article>")[0]


def test_meeting_finding_is_one_line_with_make_button(env):
    s, c = env
    meeting_finding(c)
    html = home(s)
    turn = html.split("あなたの番")[1]
    assert "柚木 茂さん: 会議の開催を調整しませんか。メールを作ります" in turn
    assert 'action="/users/U04/offer"' in turn and ">作って</button>" in turn


def test_skipped_offer_is_not_on_the_home_page(env):
    s, c = env
    meeting_finding(c)
    fp = home(s).split('name="fp" value="')[1].split('"')[0]
    TestClient(create_app(s)).post("/users/U04/offer/skip", data={"fp": fp})
    assert 'action="/users/U04/offer"' not in home(s)


def test_deadline_is_one_line_in_your_turn(env):
    s, c = env
    deadline(c)
    turn = home(s).split("あなたの番")[1]
    assert "計画の見直しの準備" in turn and "あと25日" in turn and "/desk?t=U05" in turn


def test_visit_sheet_is_one_click_from_home_and_from_the_board(env):
    s, c = env
    html = home(s)
    assert "次の訪問" in html and "/users/U04/visit" in html
    assert "/users/U04/visit" in TestClient(create_app(s)).get("/board").text



def test_cards_come_before_offers_and_offers_are_at_most_three(env):
    s, c = env
    card(c)
    for uid in ("U01", "U02", "U03", "U04", "U05"):
        save_run(c, uid, CheckRun(findings=[Finding(rule_id="R01", user_id=uid, kind="finding", period="2026-10", source="claude", message="担当者会議の記録が見当たりません。",
                                                    evidence=[Evidence(file="x.pdf", page=1, quote="月")])]))
    turn = home(s).split("あなたの番")[1]
    assert turn.count('action="/users/U0') - turn.count("/offer/skip") == 3
    assert turn.index("data-compare=") < turn.index("/offer")
    assert "ほか 2件" in turn
