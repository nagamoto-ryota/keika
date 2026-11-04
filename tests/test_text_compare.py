from fastapi.testclient import TestClient

from keika.db import connect
from keika.ledger import seed_from_json
from keika.intake import open_reviews, to_review
from keika.settings import Settings
from keika.text_compare import compare
from keika.web.app import create_app
from conftest import SAMPLE

import pytest


def marked(spans):
    return [text for text, changed in spans if changed]


def plain(spans):
    return "".join(text for text, _ in spans)


def test_small_change_marks_only_the_changed_words_on_both_sides():
    c = compare("本日訪問。古河さんが元気だったとのこと。", "本日訪問。古賀 正蔵さんが元気だったとのこと。")
    assert c.mode == "aligned"
    assert marked(c.left) == ["古河"]
    assert marked(c.right) == ["古賀 正蔵"]


def test_both_sides_keep_every_character():
    a, b = "本日訪問。\n古河さん<b>が", "本日訪問。\n古賀さん<b>が来た"
    c = compare(a, b)
    assert plain(c.left) == a and plain(c.right) == b


def test_identical_text_has_no_marks():
    c = compare("本日訪問。", "本日訪問。")
    assert not marked(c.left) and not marked(c.right)


def test_negation_in_kana_is_marked_when_the_text_is_nearly_the_same():
    c = compare("歩行器を使っている。玄関は一人で出られるとのこと。", "歩行器を使っている。玄関は一人で出られないとのこと。")
    assert marked(c.right) and c.mode == "aligned"


REWRITE_FROM = "畑江さん今日2時半から訪問で行ってきましたご主人の実さんがいらっしゃいました顔色もよくてサロン行けたって言ってました歩く練習で450メートルまでいけたそうです輝子さんは元気そんな感じです"
REWRITE_TO = "波多江氏宅へ訪問。夫の実氏が在宅。顔色は良好。サロンに行けたとのこと。歩く練習で450mまで歩けたとのこと。照子氏も99歩歩いた。"


def test_rewritten_text_marks_words_missing_from_the_other_side_not_the_style():
    c = compare(REWRITE_FROM, REWRITE_TO)
    assert c.mode == "words"
    right = "".join(marked(c.right))
    assert "波多江" in right and "照子" in right and "99" in right
    assert "とのこと" not in right and "450" not in right and "顔色" not in right and "サロン" not in right


def test_rewritten_text_marks_the_original_word_that_was_replaced():
    left = "".join(marked(compare(REWRITE_FROM, REWRITE_TO).left))
    assert "畑江" in left and "輝子" in left and "顔色" not in left


def test_a_number_does_not_pass_because_it_is_part_of_a_longer_number():
    c = compare("体重は58キロ、歩行は450メートルで、ご主人が送った話をしました。" * 2, "記録。45歩。58kg。")
    assert "45" in marked(c.right) and "58" not in marked(c.right)


def test_recorder_labels_are_not_marked():
    original = "ファイル名: Recording 2026-11-04 10-02.txt\n録音日時: 2026-11-04 10:02〜10:33（31分）\n置いた人: 真柴洋子\n\n[00:00:03] 話者1: 古河さんの歩行器を確かめました。\n\n[00:00:07] 話者2: はい上がってください。"
    c = compare(original, "居宅訪問。古賀氏の歩行器を確認した。")
    left = "".join(marked(c.left))
    assert "話者" not in left and "00" not in left and "録音日時" not in left and "ファイル名" not in left
    assert "古河" in left


def test_empty_sides_do_not_break():
    assert not marked(compare("", "").right)
    assert marked(compare("", "新しい文").right) == ["新しい文"]


@pytest.fixture
def env(tmp_path):
    s = Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "k.db")
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def item(c, original="本日訪問。古河さんが元気。", body="本日訪問。古賀 正蔵さんが元気。", warnings=(), note="「古河さん」が誰か確かめてください"):
    entry = {"user_id": "U02", "body": body, "event_date": "2026-11-04", "event_time": "", "kind_read": "居宅訪問", "monitoring": False, "partner": ""} if body else {}
    return to_review(c, original, "typed", entry, list(warnings), note)


def test_text_on_the_card_is_escaped(env):
    s, c = env
    item(c, original="<script>x</script>古河", body="<script>y</script>古賀")
    page = TestClient(create_app(s)).get("/").text
    assert "<script>x" not in page and "<script>y" not in page


def test_card_for_text_ai_could_not_tidy_does_not_call_it_tidied(env):
    s, c = env
    item(c, original="話の内容", body="", note="整えられませんでした（理由）")
    page = TestClient(create_app(s)).get("/").text
    assert "記録にする文" in page and "AI が整えた文" not in page and "話の内容" in page


def test_compare_url_recalculates_marks_for_the_edited_text(env):
    s, c = env
    rid = item(c)
    client = TestClient(create_app(s))
    data = client.post(f"/review/{rid}/compare", data={"body": "本日訪問。古河さんが\r\n元気。"}).json()
    assert data["mode"] == "aligned"
    assert "".join(t for t, _ in data["right"]) == "本日訪問。古河さんが\n元気。"
    assert client.post("/review/999/compare", data={"body": "x"}).status_code == 404


def test_saving_edited_text_still_records_it_and_compare_saves_nothing(env):
    s, c = env
    rid = item(c)
    client = TestClient(create_app(s))
    client.post(f"/review/{rid}/compare", data={"body": "途中の文"})
    assert c.execute("SELECT status FROM review_items WHERE id = ?", (rid,)).fetchone()[0] == "open"
    client.post(f"/review/{rid}", data={"user_id": "U02", "body": "直した文", "action": "save"})
    assert c.execute("SELECT status FROM review_items WHERE id = ?", (rid,)).fetchone()[0] == "resolved"
    assert c.execute("SELECT body FROM records ORDER BY id DESC LIMIT 1").fetchone()[0] == "直した文"
