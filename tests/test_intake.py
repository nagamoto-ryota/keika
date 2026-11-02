from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.intake import WireEntry, WireFix, WireIntake, invented_terms, open_reviews, process_text, resolve_review
from keika.ledger import seed_from_json
from keika.records import list_records
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def entry(**kw):
    data = dict(user_id="U04", spoken_name="柚木さん", ambiguous=False, ambiguity_note="", event_date="2026-11-04", event_time="", kind_read="居宅訪問",
                monitoring=True, partner="本人", body="柚木様宅を訪問した。変わりなし。", name_fixes=[], unsure_words=[])
    return WireEntry(**(data | kw))


def run(s, c, text, *entries, **kw):
    client = FakeClient(answer(WireIntake(entries=list(entries))))
    return process_text(c, client, s, text, "test", **kw), client


def test_dictation_becomes_a_record_marked_unchecked(env):
    s, c = env
    results, _ = run(s, c, "柚木さん宅を訪問。変わりなし。", entry())
    assert len(results) == 1 and results[0].record is not None
    r = list_records(c, "U04")[0]
    assert r.unchecked is True and r.kind_read == "居宅訪問" and r.source == "intake:test"
    assert r.first_written_at is not None


def test_two_people_in_one_dictation_become_two_records(env):
    s, c = env
    results, _ = run(s, c, "柚木さん宅を訪問。そのあと杉森さんに電話。", entry(), entry(user_id="U01", spoken_name="杉森さん", kind_read="電話", body="杉森様に電話した。"))
    assert [x.record.user_id for x in results] == ["U04", "U01"]
    assert list_records(c, "U01")[0].kind_read == "電話"


def test_ambiguous_name_goes_to_review_box(env):
    s, c = env
    # 「洋子」はケアマネの真柴洋子と、U05 の長女の戸叶洋子の両方にいる
    results, _ = run(s, c, "洋子さんから電話。", entry(user_id="U05", spoken_name="洋子さん", kind_read="電話", body="洋子さんから電話があった。"))
    assert results[0].record is None and results[0].review_id is not None
    assert list_records(c, "U05") == []
    assert len(open_reviews(c)) == 1


def test_claude_saying_ambiguous_goes_to_review_box(env):
    s, c = env
    results, _ = run(s, c, "訪問。", entry(ambiguous=True, ambiguity_note="誰の話か迷う"))
    assert results[0].review_id is not None


def test_unknown_user_goes_to_review_box(env):
    s, c = env
    results, _ = run(s, c, "田中さん宅を訪問。", entry(user_id="", spoken_name="田中さん"))
    assert results[0].review_id is not None


def test_misheard_name_corrected_from_ledger(env):
    s, c = env
    fix = [WireFix(heard="ゆの木", fixed="柚木")]
    results, _ = run(s, c, "ゆの木さん宅を訪問。変わりなし。", entry(spoken_name="柚木さん", body="柚木様宅を訪問した。変わりなし。", name_fixes=fix))
    assert results[0].record is not None and "柚木" in results[0].record.body


def test_kind_read_from_text(env):
    s, c = env
    run(s, c, "柚木さんに電話で確認。", entry(kind_read="電話", body="電話で確認した。"))
    assert list_records(c, "U04")[0].kind_read == "電話"


def test_invented_terms_detects_new_number_date_name_drug():
    assert invented_terms("訪問した", "週3回訪問した") == ["3"]
    assert invented_terms("訪問した", "11月2日に訪問した") == ["11", "2"]
    assert invented_terms("訪問した", "久留島医師に伝えた", names=["久留島"]) == ["久留島"]
    assert invented_terms("訪問した", "内服を確認し受診を勧めた") == ["内服", "受診", "受診を勧め"]


def test_invented_terms_allows_what_was_said():
    assert invented_terms("週3回に増やす", "週3回に増やす") == []
    assert invented_terms("週３回", "週3回") == []
    assert invented_terms("週三回", "週3回") == []
    assert invented_terms("久留島先生へ", "久留島医師へ", names=["久留島"]) == []
    assert invented_terms("薬を飲めていない", "薬を飲めていない") == []


def test_time_written_as_clock_is_not_invented():
    assert invented_terms("今日2時から", "14:00より") == []
    assert invented_terms("午後1時半から", "13:30より") == []
    assert invented_terms("2時から", "15:00より") == ["15"]


def test_time_written_in_kanji_form_is_not_invented():
    assert invented_terms("今日2時から", "14時より") == []
    assert invented_terms("午後1時半から", "13時30分より") == []
    assert invented_terms("2時から", "15時より") == ["15"]


def test_plain_word_turned_formal_is_not_invented():
    assert invented_terms("ふらついて転んではいない", "転倒はしていない") == []
    assert invented_terms("薬はそのまま飲んでいる", "服薬は変わらない") == []
    assert invented_terms("先生に診てもらった", "受診した") == []


def test_kana_and_short_form_of_a_ledger_name_is_not_invented():
    names = ["ぶなの木レンタル", "ヘルパーステーションほたるい", "久留島"]
    assert invented_terms("ブナの木さんに相談", "ぶなの木レンタルに相談", names=names) == []
    assert invented_terms("ほたるいの作り置き", "ヘルパーステーションほたるいの作り置き", names=names) == []
    assert invented_terms("相談した", "ぶなの木レンタルに相談した", names=names) == ["ぶなの木レンタル"]


def test_record_with_invented_terms_is_flagged_not_saved_silently(env):
    s, c = env
    results, _ = run(s, c, "訪問した。", entry(body="訪問した。週3回に増やす。"))
    assert results[0].record is None and "3" in results[0].warnings
    assert list_records(c, "U04") == []
    assert open_reviews(c)[0]["warnings"] == ["3"]


def test_name_fix_is_not_flagged_as_invented(env):
    s, c = env
    fix = [WireFix(heard="くるしま", fixed="久留島")]
    results, _ = run(s, c, "柚木さんの件でくるしま先生に電話。", entry(kind_read="電話", body="久留島医師に電話した。", name_fixes=fix))
    assert results[0].record is not None


def test_saved_record_is_unchecked_and_triggers_check(env):
    s, c = env
    called = []
    run(s, c, "柚木さん宅を訪問した。", entry(body="訪問した。"), on_saved=called.append)
    assert called == ["U04"]


def test_check_is_triggered_once_per_user(env):
    s, c = env
    called = []
    run(s, c, "柚木さん宅を訪問。電話。", entry(body="訪問。"), entry(body="電話。", kind_read="電話"), on_saved=called.append)
    assert called == ["U04"]


def test_api_failure_keeps_the_text_in_review_box(env):
    s, c = env
    results = process_text(c, FakeClient(ValueError("壊れている")), s, "大事な話。", "test")
    assert results[0].review_id is not None
    assert open_reviews(c)[0]["original"] == "大事な話。"


def test_bad_date_falls_back_to_today_with_a_warning(env):
    s, c = env
    results, _ = run(s, c, "柚木さん宅を訪問。", entry(event_date="", body="訪問。"))
    assert results[0].record.event_date == "2026-11-04"


def test_resolve_review_saves_to_chosen_user(env):
    s, c = env
    run(s, c, "洋子さんから電話。", entry(user_id="", spoken_name="洋子さん", kind_read="電話", body="洋子さんから電話があった。"))
    rid = open_reviews(c)[0]["id"]
    record = resolve_review(c, rid, user_id="U05", body="長女の洋子様から電話があった。")
    assert record.user_id == "U05" and record.body.startswith("長女")
    assert open_reviews(c) == []


def test_resolve_review_can_set_aside_without_deleting(env):
    s, c = env
    run(s, c, "訪問。", entry(user_id="", spoken_name="誰か"))
    rid = open_reviews(c)[0]["id"]
    assert resolve_review(c, rid, user_id=None) is None
    assert open_reviews(c) == []
    assert c.execute("SELECT COUNT(*) AS n FROM review_items").fetchone()["n"] == 1


def test_prompt_has_no_sample_answers():
    from keika.intake import SYSTEM
    for word in ("柚木", "杉森", "洋子", "T04"):
        assert word not in SYSTEM


# --- 画面 ---


def app_client(s, wire):
    return TestClient(create_app(s, client_factory=lambda: FakeClient(answer(wire))))


def test_new_record_page_has_voice_button_and_notice(env):
    s, c = env
    body = TestClient(create_app(s)).get("/records/new").text
    assert "speech.js" in body and "話す" in body and "音声" in body


def test_new_record_post_shows_result(env):
    s, c = env
    body = app_client(s, WireIntake(entries=[entry(body="訪問した。")])).post("/records/new", data={"text": "柚木さん宅を訪問した。", "mode": "shape"}).text
    assert "訪問した。" in body and "柚木 茂" in body


def test_empty_text_is_refused(env):
    s, c = env
    assert TestClient(create_app(s)).post("/records/new", data={"text": " ", "mode": "shape"}).status_code == 400


def test_review_page_lists_and_resolves(env):
    s, c = env
    run(s, c, "洋子さんから電話。", entry(user_id="", spoken_name="洋子さん", kind_read="電話", body="洋子さんから電話があった。"))
    client = TestClient(create_app(s))
    page = client.get("/review").text
    assert "洋子さんから電話があった。" in page and "柚木 茂" in page
    rid = open_reviews(c)[0]["id"]
    r = client.post(f"/review/{rid}", data={"user_id": "U05", "body": "長女から電話があった。", "action": "save"}, follow_redirects=False)
    assert r.status_code == 303 and open_reviews(c) == []
    assert list_records(c, "U05")[0].body == "長女から電話があった。"


def test_home_shows_review_count(env):
    s, c = env
    run(s, c, "洋子さんから電話。", entry(user_id="", spoken_name="洋子さん"))
    assert "記録の確認" in TestClient(create_app(s)).get("/").text
