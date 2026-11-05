"""確認カード: 1文の提案、〔違う〕で直す、「これ足して」、通し読みの理由、相談の見出し。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.intake import WireEntry, WireFix, _calls_this_user, open_reviews, original_of, resolve_review, to_review
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record, list_records
from keika.review_card import WireReason, card_view, find_reason, revise
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


FIXES = [{"heard": "古河さん", "fixed": "古賀 正蔵"}, {"heard": "輝子さん", "fixed": "古賀 照子"}, {"heard": "竜也", "fixed": "古賀 達也"}, {"heard": "苦島先生", "fixed": "久留島 誠"}]


def raw_entry(**kw):
    return {"user_id": "U02", "spoken_name": "古河さん", "ambiguous": False, "ambiguity_note": "", "event_date": "2026-11-04", "event_time": "", "kind_read": "居宅訪問",
            "monitoring": False, "partner": "本人", "body": "古賀様宅を訪問した。照子氏と話した。", "name_fixes": FIXES, "unsure_words": []} | kw


def card(c, original="古河さん宅を訪問。輝子さんと話した。", entry=None, warnings=(), note="「古河さん」が誰か確かめてください"):
    to_review(c, original, "typed", raw_entry() if entry is None else entry, list(warnings), note)
    return open_reviews(c)[-1]


def wire(**kw):
    return WireEntry(**(raw_entry(name_fixes=[]) | kw))


def page(s, **kw):
    return TestClient(create_app(s, **kw)).get("/todo").text


def fake(result):
    return FakeClient(answer(result))


# 1. 1文の提案
def test_card_is_one_sentence_proposal(env):
    s, c = env
    view = card_view(c, card(c))
    assert view["sentence"] == "古河さんの話は、古賀 正蔵さんの記録にします。"
    assert view["doubt"] == ""  # 決まり文句の「誰か確かめて」は提案で分かる


def test_card_does_not_list_other_name_fixes(env):
    s, c = env
    card(c)
    html = page(s)
    top = html.split('class="compare-fold"')[0]
    assert "古河さんの話は、古賀 正蔵さんの記録にします。" in html
    for other in ("竜也", "苦島先生", "judge-table", "記録での書き方"):
        assert other not in top
    assert ">いいよ</button>" in html and "<summary>違う</summary>" in html and "記録にしない" in html


def test_ai_doubt_is_shown(env):
    s, c = env
    view = card_view(c, card(c, note="相手が本人か家族か話から分かりません。"))
    assert view["doubt"] == "相手が本人か家族か話から分かりません。"


def test_no_person_asks_to_choose(env):
    s, c = env
    view = card_view(c, card(c, entry=raw_entry(user_id="", spoken_name="田中さん", name_fixes=[])))
    assert view["needs_choice"] and "田中さん" in view["sentence"] and "分かりません" in view["sentence"]
    assert '<details class="ng" open>' in page(s)


def test_untidied_talk_opens_the_text(env):
    s, c = env
    view = card_view(c, card(c, entry={}, note="整えられませんでした（理由）"))
    assert view["needs_choice"] and view["sentence"] == "整えられませんでした（理由）"
    assert '<details class="compare-fold" open>' in page(s)


def test_card_form_saves_with_the_person(env):
    s, c = env
    it = card(c)
    r = TestClient(create_app(s)).post(f"/review/{it['id']}", data={"user_id": "U02", "body": it["entry"]["body"], "action": "save"}, follow_redirects=False)
    assert r.status_code == 303 and any("照子氏" in x.body for x in list_records(c, "U02"))


def test_set_aside_still_works_from_the_card(env):
    s, c = env
    it = card(c)
    TestClient(create_app(s)).post(f"/review/{it['id']}", data={"user_id": "U02", "body": "x", "action": "set_aside"}, follow_redirects=False)
    assert open_reviews(c) == []


# 5. 相談の見出し
def test_consult_heading_comes_first(env):
    s, c = env
    card(c)
    html = page(s)
    assert "ケイカからの相談" in html and "記録の確認" in html
    assert html.index("ケイカからの相談") < html.index("ほかの項目")


def test_other_items_show_when_nothing_to_consult(env):
    s, c = env
    assert 'class="minor"' not in page(s)


# 3. これ足して
def test_number_is_asked_with_the_sentence_around_it(env):
    s, c = env
    view = card_view(c, card(c, entry=raw_entry(body="歩行器で99歩歩いた。"), warnings=["99"], note=""))
    assert view["question"] == "「歩行器で99歩歩いた」の 99 は入力にありません。合っていますか。" and view["word"] == "99"


def test_time_and_date_and_visit_are_asked(env):
    s, c = env
    assert "何時" in card_view(c, card(c, warnings=["15:00"]))["question"]
    assert "いつ" in card_view(c, card(c, warnings=["2026-11-05"]))["question"]
    assert "訪問" in card_view(c, card(c, warnings=["居宅訪問"]))["question"]


def test_name_is_asked(env):
    s, c = env
    assert "どなた" in card_view(c, card(c, warnings=["達也"]))["question"]


@pytest.mark.parametrize("word,body", [("アムロジピン", "アムロジピンを飲んでいる。"), ("5mg", "薬は5mgとのこと。"), ("受診", "来週受診する。"), ("脳梗塞", "脳梗塞と思われる。")])
def test_medical_words_are_never_asked(env, word, body):
    s, c = env
    view = card_view(c, card(c, entry=raw_entry(body=body), warnings=[word], note=""))
    assert view["question"] == "" and any("入力にありません" in x and word in x for x in view["cautions"])


def test_dose_number_is_not_asked(env):
    s, c = env
    view = card_view(c, card(c, entry=raw_entry(body="薬は5mgとのこと。"), warnings=["5", "5mg"], note=""))
    assert view["question"] == "" and len(view["cautions"]) == 2


def test_only_one_question_per_card(env):
    s, c = env
    view = card_view(c, card(c, entry=raw_entry(body="99歩歩き、88歩休んだ。"), warnings=["99", "88"], note=""))
    assert view["word"] == "99" and view["cautions"] == []


def test_yes_confirms_the_word_without_ai(env):
    s, c = env
    it = card(c, entry=raw_entry(body="99歩歩いた。"), warnings=["99"], note="")
    client = fake(wire())
    assert revise(c, client, s, it["id"], answer_text="はい") == ""
    assert client.calls == []
    now = open_reviews(c)[-1]
    assert now["warnings"] == [] and now["entry"]["confirmed"] == ["99"]


def test_other_answer_goes_to_ai_and_only_the_answer_counts_as_said(env):
    s, c = env
    it = card(c, entry=raw_entry(body="15時に訪問した。", event_time="15:00"), warnings=["15"], note="")
    assert revise(c, fake(wire(body="10時に訪問した。", event_time="10:00")), s, it["id"], answer_text="10時です") == ""
    now = open_reviews(c)[-1]
    assert now["entry"]["body"] == "10時に訪問した。" and now["warnings"] == [] and now["entry"]["instructions"] == ["10時です"]


def test_question_text_does_not_count_as_said(env):
    s, c = env
    it = card(c, entry=raw_entry(body="99歩歩いた。"), warnings=["99"], note="")
    assert revise(c, fake(wire(body="99歩歩いた。")), s, it["id"], answer_text="よく分からない") == ""
    assert open_reviews(c)[-1]["warnings"] == ["99"]


def test_answer_is_all_that_is_added_to_the_prompt_as_said(env):
    s, c = env
    it = card(c, entry=raw_entry(body="99歩歩いた。"), warnings=["99"], note="")
    client = fake(wire(body="99歩歩いた。"))
    revise(c, client, s, it["id"], answer_text="よく分からない")
    text = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "よく分からない" in text and "合っていますか" in text


# 2. 違う→自由記述
def test_instruction_updates_the_proposal_and_stays_on_the_card(env):
    s, c = env
    it = card(c)
    assert revise(c, fake(wire(user_id="U03", body="訪問した。")), s, it["id"], instruction="綿貫さんの話です") == ""
    now = open_reviews(c)[-1]
    assert now["id"] == it["id"] and now["entry"]["user_id"] == "U03" and now["entry"]["spoken_name"] == "古河さん" and now["note"] == ""
    assert card_view(c, now)["sentence"] == "古河さんの話は、綿貫 キヨさんの記録にします。"
    assert list_records(c, "U03") == []


def test_card_says_what_changed_after_revise(env):
    s, c = env
    it = card(c, entry=raw_entry(body="相談員の浜野氏へ連絡する。"))
    revise(c, fake(wire(body="相談員の浜埜氏へ連絡する。")), s, it["id"], instruction="浜埜の字です")
    assert "浜野" in card_view(c, open_reviews(c)[-1])["changed"] and "浜埜" in card_view(c, open_reviews(c)[-1])["changed"]
    assert "直しました" in page(s)


def test_same_change_in_two_places_is_said_once(env):
    s, c = env
    it = card(c, entry=raw_entry(body="浜野氏へ連絡。浜野氏より返事。"))
    revise(c, fake(wire(body="浜埜氏へ連絡。浜埜氏より返事。")), s, it["id"], instruction="浜埜の字です")
    assert card_view(c, open_reviews(c)[-1])["changed"] == "「浜野氏」を「浜埜氏」に直しました。"


def test_card_says_when_nothing_changed(env):
    s, c = env
    it = card(c, entry=raw_entry(body="訪問した。"))
    revise(c, fake(wire(body="訪問した。")), s, it["id"], instruction="直して")
    assert card_view(c, open_reviews(c)[-1])["changed"] == "文は変わりませんでした。"


def test_many_changes_are_not_listed(env):
    s, c = env
    it = card(c, entry=raw_entry(body="今日は寒い。庭に出た。犬がいた。夜に雨。"))
    revise(c, fake(wire(body="昨日は暑い。畑に行った。猫がいた。朝に雪。")), s, it["id"], instruction="本日分に直して")
    assert card_view(c, open_reviews(c)[-1])["changed"] == "文を直しました。全文で確かめられます。"


def test_fact_not_in_talk_or_instruction_is_rejected(env):
    s, c = env
    it = card(c)
    before = it["entry"]["body"]
    notice = revise(c, fake(wire(body="訪問した。歩行は99歩だった。")), s, it["id"], instruction="字を直して")
    assert "99" in notice and "直しませんでした" in notice
    now = open_reviews(c)[-1]
    assert now["entry"]["body"] == before and "instructions" not in now["entry"]


def test_fact_in_the_instruction_is_allowed(env):
    s, c = env
    it = card(c)
    assert revise(c, fake(wire(body="訪問した。歩行は99歩だった。")), s, it["id"], instruction="99歩歩いたと足して") == ""
    assert "99歩" in open_reviews(c)[-1]["entry"]["body"]


def test_medicine_not_said_is_rejected_after_revise(env):
    s, c = env
    it = card(c)
    notice = revise(c, fake(wire(body="訪問した。薬はアムロジピンを飲んでいる。")), s, it["id"], instruction="服薬も書いて")
    assert "アムロジピン" in notice and "アムロジピン" not in open_reviews(c)[-1]["entry"]["body"]


def test_unknown_person_from_ai_keeps_the_old_person(env):
    s, c = env
    it = card(c)
    revise(c, fake(wire(user_id="U99", body="訪問した。")), s, it["id"], instruction="直して")
    assert open_reviews(c)[-1]["entry"]["user_id"] == "U02"


def test_ai_failure_keeps_the_card(env):
    s, c = env
    it = card(c)
    assert "直せませんでした" in revise(c, fake(None), s, it["id"], instruction="直して")
    assert open_reviews(c)[-1]["entry"]["body"] == it["entry"]["body"]


def test_edited_body_is_what_gets_revised(env):
    s, c = env
    it = card(c)
    client = fake(wire(body="訪問した。"))
    revise(c, client, s, it["id"], instruction="直して", body="手で直した文")
    assert "手で直した文" in client.calls[0]["messages"][0]["content"][0]["text"]


def test_revise_route_shows_the_notice_once(env):
    s, c = env
    it = card(c)
    tc = TestClient(create_app(s, client_factory=lambda: fake(wire(body="訪問した。歩行は99歩だった。"))))
    r = tc.post(f"/review/{it['id']}/revise", data={"instruction": "直して", "user_id": "U02", "body": it["entry"]["body"]}, follow_redirects=False)
    assert r.status_code == 303
    assert "直しませんでした" in tc.get("/todo").text
    assert "直しませんでした" not in tc.get("/todo").text


def test_written_text_with_ok_button_is_not_lost(env):
    s, c = env
    it = card(c)
    tc = TestClient(create_app(s, client_factory=lambda: fake(wire(body="訪問した。"))))
    tc.post(f"/review/{it['id']}", data={"instruction": "直して", "user_id": "U02", "body": it["entry"]["body"], "action": "save"}, follow_redirects=False)
    assert open_reviews(c)[-1]["entry"]["body"] == "訪問した。"
    assert not any(x.body == "訪問した。" for x in list_records(c, "U02"))


def test_revise_without_text_changes_nothing(env):
    s, c = env
    it = card(c)
    client = fake(None)
    assert revise(c, client, s, it["id"], instruction="  ") == ""
    assert client.calls == [] and open_reviews(c)[-1]["entry"] == it["entry"]


def test_sample_mode_cannot_revise(env):
    s, c = env
    it = card(c)
    assert TestClient(create_app(s, sample=True)).post(f"/review/{it['id']}/revise", data={"instruction": "x"}).status_code == 403


def test_no_api_key_keeps_the_card_and_says_so(env):
    s, c = env
    it = card(c)

    def no_key():
        raise RuntimeError("API キーがありません")

    tc = TestClient(create_app(s, client_factory=no_key))
    tc.post(f"/review/{it['id']}/revise", data={"instruction": "直して"}, follow_redirects=False)
    assert "直せませんでした" in tc.get("/todo").text and open_reviews(c)[-1]["entry"] == it["entry"]


def test_saved_record_keeps_what_the_person_wrote_as_the_source(env):
    s, c = env
    it = card(c)
    revise(c, fake(wire(body="訪問した。")), s, it["id"], instruction="短くして")
    record = resolve_review(c, it["id"], user_id="U02", s=s)
    source = original_of(c, record.id)
    assert it["original"] in source and "短くして" in source


# 4. 通し読みの理由
def past(c, body, user="U02"):
    return add_record(c, RecordIn(user_id=user, event_date="2026-10-01", event_time="", kind_written="電話", kind_read="電話", monitoring=False, partner="", body=body, author="", source="intake:test"))


def test_reason_uses_words_found_in_both_talk_and_past(env):
    s, c = env
    past(c, "歩行器の相談あり。照子氏より電話。")
    it = card(c, original="古河さん宅を訪問。輝子さんと話した。歩行器は問題なし。")
    assert find_reason(c, fake(WireReason(words=["歩行器"])), s, it["id"]) == "「歩行器」が、前の記録や書類にも出ています。"


def test_word_only_in_ai_answer_is_dropped(env):
    s, c = env
    past(c, "歩行器の相談あり。")
    it = card(c)  # 話に歩行器は無い
    assert find_reason(c, fake(WireReason(words=["歩行器", "夢の話"])), s, it["id"]) == ""


def test_word_common_to_many_others_is_dropped(env):
    s, c = env
    past(c, "歩行器の相談あり。")
    for user in ("U01", "U03", "U04", "U05", "U06"):
        past(c, "歩行器を使っている。", user)
    it = card(c, original="古河さん宅。歩行器は問題なし。")
    assert find_reason(c, fake(WireReason(words=["歩行器"])), s, it["id"]) == ""


def test_reason_is_kept_and_not_asked_twice(env):
    s, c = env
    past(c, "歩行器の相談あり。")
    it = card(c, original="古河さん。歩行器は問題なし。")
    find_reason(c, fake(WireReason(words=["歩行器"])), s, it["id"])
    again = fake(None)
    assert find_reason(c, again, s, it["id"]) == "「歩行器」が、前の記録や書類にも出ています。" and again.calls == []
    assert card_view(c, open_reviews(c)[-1])["reason"] == "「歩行器」が、前の記録や書類にも出ています。"


def test_reason_failure_writes_nothing(env):
    s, c = env
    it = card(c)
    assert find_reason(c, fake(None), s, it["id"]) == ""
    assert card_view(c, open_reviews(c)[-1])["reason"] == ""


def test_no_reason_without_a_person(env):
    s, c = env
    it = card(c, entry=raw_entry(user_id=""))
    client = fake(WireReason(words=["古賀"]))
    assert find_reason(c, client, s, it["id"]) == "" and client.calls == []


def test_revise_with_new_person_clears_the_reason(env):
    s, c = env
    past(c, "歩行器の相談あり。")
    it = card(c, original="古河さん。歩行器は問題なし。")
    find_reason(c, fake(WireReason(words=["歩行器"])), s, it["id"])
    revise(c, fake(wire(user_id="U03", body="訪問した。")), s, it["id"], instruction="綿貫さんの訪問です")
    assert "reason" not in open_reviews(c)[-1]["entry"]


def test_only_this_persons_material_goes_to_ai(env):
    s, c = env
    past(c, "自分の記録")
    past(c, "他の人の秘密の記録", "U03")
    it = card(c, original="古河さん。")
    client = fake(WireReason(words=[]))
    find_reason(c, client, s, it["id"])
    sent = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "自分の記録" in sent and "他の人の秘密の記録" not in sent


def test_reason_route(env):
    s, c = env
    past(c, "歩行器の相談あり。")
    it = card(c, original="古河さん。歩行器は問題なし。")
    tc = TestClient(create_app(s, client_factory=lambda: fake(WireReason(words=["歩行器"]))))
    assert "歩行器" in tc.post(f"/review/{it['id']}/reason").json()["reason"]
    assert "歩行器" in TestClient(create_app(s, sample=True)).get("/todo").text  # 保存済みの理由は見本でも読める


def test_stop_rule_itself_is_unchanged(env):
    """古河さんと報告されたら止まり、古河と報告されたら記録される。今回の範囲では変えない（README に書いてある）。"""
    s, c = env
    assert not _calls_this_user(c, "古河さん", "U02", [WireFix(heard="古河さん", fixed="古賀 正蔵")])
    assert _calls_this_user(c, "古河さん", "U02", [WireFix(heard="古河", fixed="古賀 正蔵")])


def test_snapshot_tool_counts_review_cards(env):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("build_snapshot", Path(__file__).parent.parent / "tools" / "build_snapshot.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    s, c = env
    assert tool.review_cards(c) == 0
    card(c)
    assert tool.review_cards(c) == 1
