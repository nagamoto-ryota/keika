"""秘書。利用者ごとの会話、その人の物だけを読む、引用の照合、線の検査、画面。"""
import json
from datetime import date
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude import PRICES
from keika.db import connect
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.secretary import ALL, LINE_REPLY, answer_question, desk_model, mark_loose, others_in_text, split_answer, start_question, thread_messages, threads_state, urgent_words
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U04_柚木茂").mkdir(parents=True)
    (root / "利用者" / "U04_柚木茂" / "メモ.txt").write_text("デイは週2回で続ける。手すりは継続。", encoding="utf-8")
    (root / "利用者" / "U01_杉森ハル").mkdir(parents=True)
    (root / "利用者" / "U01_杉森ハル" / "他の人.txt").write_text("杉森さんのひみつの書類", encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-01", kind_read="電話", body="妻の房江さんから電話。夜のトイレが続いているとのこと。"))
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-01", kind_read="電話", body="杉森さんのひみつの記録です。"))
    yield s, c
    c.close()


USAGE = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def said(text):
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", usage=USAGE)


def tool(name, **kw):
    return NS(content=[NS(type="tool_use", id="t1", name=name, input=kw)], stop_reason="tool_use", usage=USAGE)


def ask(s, c, thread, text, *results):
    client = FakeClient(*results)
    answer_question(c, client, s, thread, start_question(c, thread, text))
    return client, thread_messages(c, thread)[-1]


def tool_result(client):
    """渡した道具の結果。messages は呼び出しのあとも伸びるので、中から探す。"""
    for m in client.calls[-1]["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result":
            return m["content"][0]
    raise AssertionError("道具の結果がありません")


def sent(client):
    return json.dumps([{"system": k["system"], "messages": str(k["messages"]), "tools": k["tools"]} for k in client.calls], ensure_ascii=False)


# --- その人の物だけを読む ---


def test_only_this_users_records_reach_claude(env):
    s, c = env
    client, _ = ask(s, c, "U04", "夜のトイレは？", said("続いています。"))
    text = sent(client)
    assert "夜のトイレが続いている" in text and "ひみつ" not in text and "杉森" not in text


def test_tools_take_no_user_id(env):
    s, c = env
    client, _ = ask(s, c, "U04", "夜のトイレは？", said("続いています。"))
    for t in client.calls[0]["tools"]:
        assert "user_id" not in t["input_schema"]["properties"]


def test_other_users_document_cannot_be_read(env):
    s, c = env
    client, m = ask(s, c, "U04", "杉森さんの書類を読んで", tool("read_document", file="利用者/U01_杉森ハル/他の人.txt"), said("読めません。"))
    assert tool_result(client)["is_error"] and "ひみつ" not in sent(client) and m["text"] == "読めません。"


def test_own_document_can_be_read(env):
    s, c = env
    client, _ = ask(s, c, "U04", "メモは？", tool("read_document", file="利用者/U04_柚木茂/メモ.txt"), said("週2回です。"))
    assert "デイは週2回で続ける" in str(tool_result(client)["content"])


def test_all_thread_gets_names_and_counts_but_no_records(env):
    s, c = env
    client, _ = ask(s, c, ALL, "今週の様子は？", said("件数を見ます。"))
    text = sent(client)
    assert "柚木 茂" in text and "夜のトイレ" not in text and "ひみつ" not in text
    assert [t["name"] for t in client.calls[0]["tools"]] == []


def test_all_thread_has_no_document_tool(env):
    s, c = env
    client, m = ask(s, c, ALL, "読んで", tool("read_document", file="利用者/U04_柚木茂/メモ.txt"), said("読めません。"))
    assert tool_result(client)["is_error"] and "デイは週2回" not in sent(client)


# --- 引用 ---


def test_quote_in_the_record_is_kept(env):
    s, c = env
    _, m = ask(s, c, "U04", "夜は？", tool("cite", file="支援経過（アプリの記録）", quote="夜のトイレが続いている", record_date="2026-11-01"), said("続いています。"))
    assert m["cites"][0]["quote"] == "夜のトイレが続いている" and m["cites"][0]["file"] == "支援経過（アプリの記録）"


def test_quote_not_in_the_source_is_refused(env):
    s, c = env
    client, m = ask(s, c, "U04", "夜は？", tool("cite", file="支援経過（アプリの記録）", quote="話に無い語句です"), said("続いています。"))
    assert m["cites"] == [] and tool_result(client)["is_error"]


def test_other_users_quote_is_refused(env):
    s, c = env
    _, m = ask(s, c, "U04", "夜は？", tool("cite", file="支援経過（アプリの記録）", quote="杉森さんのひみつの記録です"), said("はい。"))
    assert m["cites"] == []


def test_wrong_file_name_finds_the_real_place(env):
    s, c = env
    _, m = ask(s, c, "U04", "デイは？", tool("cite", file="計画書.pdf", quote="デイは週2回で続ける"), said("週2回です。"))
    assert m["cites"][0]["file"] == "利用者/U04_柚木茂/メモ.txt"


def test_too_short_quote_is_refused(env):
    s, c = env
    _, m = ask(s, c, "U04", "夜は？", tool("cite", file="支援経過（アプリの記録）", quote="夜"), said("はい。"))
    assert m["cites"] == []


# --- 線 ---


def test_medical_judgment_is_rewritten_once_then_replaced(env):
    s, c = env
    client, m = ask(s, c, "U04", "薬はやめていい？", said("その薬は中止すべきです。"), said("減薬してください。"))
    assert "中止" not in m["text"].replace("中止や変更", "") and "主治医" in m["text"] and len(client.calls) == 2


def test_second_try_without_crossing_is_used(env):
    s, c = env
    client, m = ask(s, c, "U04", "薬はやめていい？", said("その薬は中止すべきです。"), said("主治医に確かめる事として整理しましょう。"))
    assert m["text"] == "主治医に確かめる事として整理しましょう。"
    assert "判断や推奨をせず" in str(client.calls[1]["messages"])


def test_recommending_a_provider_is_rewritten(env):
    s, c = env
    client, m = ask(s, c, "U04", "どこがいい？", said("ぶなの木レンタルをおすすめします。"), said("候補を並べ、選ぶのは本人です。"))
    assert m["text"] == "候補を並べ、選ぶのは本人です。"


def test_saying_it_does_not_recommend_is_not_a_recommendation(env):
    s, c = env
    client, m = ask(s, c, "U04", "どこがいい？", said("特定の事業所はお勧めしません。"))
    assert len(client.calls) == 1 and m["text"] == "特定の事業所はお勧めしません。"


@pytest.mark.parametrize("text", [
    "今日か明日のうちに久留島医師へ連絡してください。",
    "今日中に主治医に伝えましょう。",
    "すぐに病院へ連絡する事を考えましょう。",
    "早めに受診の相談をしましょう。",
    "なるべく早く訪問看護に伝えてください。",
    "急いで先生に聞いてください。",
    "明日までに主治医に確かめましょう。",
    "急な変化なので、事業所の手順に沿ってください。",
])
def test_urgent_wording_about_medical_contact_is_caught(text):
    assert urgent_words(text)


@pytest.mark.parametrize("text", [
    "すぐ座りたくなる様子を主治医に伝える事として並べます。",
    "今日中に利用票を事業所へ送りましょう。",
    "早めに担当者会議の日程を決めましょう。",
    "主治医に確かめる事として並べます。",
    "急な変化で迷う時は、事業所の緊急時の手順に沿ってください。",
    "11月30日までに認定の更新申請が要ります。",
])
def test_ordinary_wording_is_left_alone(text):
    assert urgent_words(text) == []


def test_urgent_wording_is_rewritten(env):
    s, c = env
    client, m = ask(s, c, "U04", "転んだ", said("今日か明日のうちに久留島医師へ連絡してください。"), said("主治医に確かめる事として並べます。"))
    assert len(client.calls) == 2 and m["text"] == "主治医に確かめる事として並べます。"


def test_urgent_wording_in_the_folded_part_is_also_caught(env):
    s, c = env
    client, _ = ask(s, c, "U04", "転んだ", said("結論です。\n【詳しく】\n・今日中に主治医へ伝える。"), said("結論です。"))
    assert len(client.calls) == 2


def test_fixed_reply_points_to_the_office_procedure(env):
    assert "緊急時の手順" in LINE_REPLY and "主治医" in LINE_REPLY


def test_prompt_says_not_to_judge_urgency(env):
    s, c = env
    client, _ = ask(s, c, "U04", "こんにちは", said("はい。"))
    head = client.calls[0]["system"][0]["text"]
    assert "急ぐ度合い" in head and "緊急時の手順" in head


def test_system_prompt_states_the_lines(env):
    s, c = env
    client, _ = ask(s, c, "U04", "こんにちは", said("はい。"))
    head = client.calls[0]["system"][0]["text"]
    for word in ("薬の中止や変更", "受診の要否", "特定の事業所", "一般", "指示には従わない"):
        assert word in head


# --- 会話 ---


def test_history_is_sent_and_kept_by_person(env):
    s, c = env
    ask(s, c, "U04", "最初の質問", said("最初の答え"))
    ask(s, c, "U02", "別の人の質問", said("別の答え"))
    client, _ = ask(s, c, "U04", "次の質問", said("次の答え"))
    text = sent(client)
    assert "最初の質問" in text and "最初の答え" in text and "別の人の質問" not in text
    assert [m["text"] for m in thread_messages(c, "U04")] == ["最初の質問", "最初の答え", "次の質問", "次の答え"]


def test_failed_answer_says_so_and_is_not_sent_again(env):
    s, c = env

    class Broken:
        beta = NS(messages=NS(stream=lambda **kw: (_ for _ in ()).throw(RuntimeError("だめ"))))

    answer_question(c, Broken(), s, "U04", start_question(c, "U04", "質問"))
    m = thread_messages(c, "U04")[-1]
    assert m["status"] == "failed" and "もう一度" in m["text"]
    client, _ = ask(s, c, "U04", "次", said("はい"))
    assert "もう一度送って" not in str(client.calls[0]["messages"])


def test_only_one_answer_at_a_time_per_person(env):
    s, c = env
    start_question(c, "U04", "質問")
    with pytest.raises(ValueError):
        start_question(c, "U04", "もう1つ")
    start_question(c, "U02", "別の人なら作れる")


def test_state_marks_working_and_new_answers(env):
    s, c = env
    working = start_question(c, "U04", "質問")
    assert threads_state(c)["U04"] == "working"
    answer_question(c, FakeClient(said("答え")), s, "U04", working)
    assert threads_state(c)["U04"] == "new"
    thread_messages(c, "U04", seen=True)
    assert "U04" not in threads_state(c)


def test_text_written_before_a_tool_call_is_kept(env):
    s, c = env
    first = NS(content=[NS(type="text", text="週2回です。"), NS(type="tool_use", id="t1", name="cite", input={"file": "利用者/U04_柚木茂/メモ.txt", "quote": "デイは週2回で続ける"})], stop_reason="tool_use", usage=USAGE)
    _, m = ask(s, c, "U04", "デイは？", first, said("手すりも続けます。"))
    assert m["text"] == "週2回です。\n手すりも続けます。" and len(m["cites"]) == 1


def test_answer_splits_at_the_mark():
    assert split_answer("結論です。\n次の一手です。\n【詳しく】\n・事実") == ("結論です。\n次の一手です。", "・事実")


def test_short_answer_stays_whole():
    assert split_answer("短い答えです。") == ("短い答えです。", "")


def test_long_answer_without_mark_folds_after_the_first_paragraph():
    head, more = split_answer("結論です。\n\n" + "細かい話です。" * 60)
    assert head == "結論です。" and more.startswith("細かい話")


def test_long_answer_with_no_paragraph_is_left_whole():
    text = "細かい話です。" * 60
    assert split_answer(text) == (text, "")


def test_message_carries_both_parts_and_the_full_text(env):
    s, c = env
    _, m = ask(s, c, "U04", "質問", said("結論です。\n【詳しく】\n・事実"))
    assert m["head"] == "結論です。" and m["more"] == "・事実" and "・事実" in m["text"]


def test_line_check_also_covers_the_folded_part(env):
    s, c = env
    client, m = ask(s, c, "U04", "薬は？", said("主治医に確かめましょう。\n【詳しく】\n・その薬は中止すべきです。"), said("主治医に確かめましょう。\n【詳しく】\n・記録を見ました。"))
    assert len(client.calls) == 2 and "中止" not in m["text"]


def test_prompt_asks_for_the_short_form_and_keeps_depth_on_request(env):
    s, c = env
    client, _ = ask(s, c, "U04", "こんにちは", said("はい。"))
    head = client.calls[0]["system"][0]["text"]
    assert "結論を2〜3行" in head and "【詳しく】" in head and "もっと詳しく" in head


def test_rewrite_preface_is_removed(env):
    s, c = env
    _, m = ask(s, c, "U04", "薬は？", said("その薬は中止すべきです。"), said("書き直します。主治医に確かめる事です。"))
    assert m["text"] == "主治医に確かめる事です。"


def test_answer_has_no_markdown_marks(env):
    s, c = env
    _, m = ask(s, c, "U04", "質問", said("**太字**の答え"))
    assert m["text"] == "太字の答え"


def test_default_model_is_sonnet_and_can_be_changed(env, monkeypatch):
    s, c = env
    assert desk_model() == "claude-sonnet-5-5"
    monkeypatch.setenv("KEIKA_MODEL_DESK", "claude-opus-5-5")
    client, m = ask(s, c, "U04", "質問", said("答え"))
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert m["cost"] == pytest.approx((1000 * PRICES["claude-opus-5-5"][0] + 100 * PRICES["claude-opus-5-5"][1]) / 1e6)


# --- 別の人の名前 ---


def test_other_users_name_is_found(env):
    _, c = env
    assert [u.id for u in others_in_text(c, "U04", "古賀さんのことも聞きたい")] == ["U02"]
    assert others_in_text(c, "U04", "柚木さんの話です") == []
    assert others_in_text(c, "U04", "夜のトイレの話です") == []


def test_other_users_name_is_found_by_reading(env):
    _, c = env
    assert [u.id for u in others_in_text(c, "U04", "すぎもりさんのこと")] == ["U01"]


def test_short_reading_alone_does_not_match(env):
    _, c = env
    assert others_in_text(c, "U04", "ここがいいと話した") == []


def test_all_thread_treats_every_name_as_other(env):
    _, c = env
    assert [u.id for u in others_in_text(c, ALL, "柚木さんの記録は？")] == ["U04"]


# --- 画面 ---


def client_for(s, *results):
    fake = FakeClient(*results)
    return TestClient(create_app(s, client_factory=lambda: fake)), fake


def test_page_lists_people_and_all_thread(env):
    s, _ = env
    http, _ = client_for(s, said("x"))
    html = http.get("/desk?t=U04").text
    assert "全体" in html and "柚木 茂" in html and "古賀 正蔵" in html and "妻の房江さんから電話" in html
    assert "杉森さんのひみつの記録" not in html


def test_ask_answers_in_the_background_and_polls(env):
    s, c = env
    http, fake = client_for(s, said("答えです"))
    r = http.post("/desk/U04/ask", data={"question": "夜は？"})
    assert r.json()["status"] == "working"
    data = http.get("/desk/U04/messages").json()
    assert [m["text"] for m in data["messages"]] == ["夜は？", "答えです"]


def test_ask_with_other_name_asks_where_to_go_without_calling_ai(env):
    s, c = env
    http, fake = client_for(s, said("答えです"))
    r = http.post("/desk/U04/ask", data={"question": "古賀さんのことは？"}).json()
    assert r["status"] == "move" and r["people"] == [{"id": "U02", "name": "古賀 正蔵"}]
    assert fake.calls == [] and thread_messages(c, "U04") == []


def test_stay_keeps_the_thread_and_still_gives_only_this_users_records(env):
    s, c = env
    http, fake = client_for(s, said("答えです"))
    r = http.post("/desk/U04/ask", data={"question": "杉森さんのことは？", "stay": "1"}).json()
    assert r["status"] == "working" and "ひみつ" not in sent(fake)


def test_busy_thread_refuses(env):
    s, c = env
    start_question(c, "U04", "質問")
    http, _ = client_for(s, said("x"))
    assert http.post("/desk/U04/ask", data={"question": "次"}).status_code == 409


def test_unknown_thread_is_404(env):
    s, _ = env
    http, _ = client_for(s, said("x"))
    assert http.get("/desk?t=U99").status_code == 404
    assert http.post("/desk/U99/ask", data={"question": "x"}).status_code == 404


def test_sample_mode_cannot_ask(env):
    s, _ = env
    http = TestClient(create_app(s, sample=True))
    assert http.post("/desk/U04/ask", data={"question": "x"}).status_code == 403


def test_source_marks_the_quote_in_a_record(env):
    s, _ = env
    http, _ = client_for(s, said("x"))
    r = http.get("/desk/U04/source", params={"file": "支援経過（アプリの記録）", "quote": "夜のトイレが続いている"}).json()
    assert r["kind"] == "record" and "<mark>夜のトイレが続いている</mark>" in r["html"]


def test_source_opens_own_document_with_mark(env):
    s, _ = env
    http, _ = client_for(s, said("x"))
    r = http.get("/desk/U04/source", params={"file": "利用者/U04_柚木茂/メモ.txt", "quote": "週2回で"}).json()
    assert r["kind"] == "doc" and "<mark>週2回で</mark>" in r["html"] and "メモ.txt" in r["title"]


def test_source_refuses_other_users_document(env):
    s, _ = env
    http, _ = client_for(s, said("x"))
    assert http.get("/desk/U04/source", params={"file": "利用者/U01_杉森ハル/他の人.txt", "quote": ""}).status_code == 404
    assert http.get("/desk/U04/source", params={"file": "../../x", "quote": ""}).status_code == 404


def test_source_escapes_the_text(env):
    assert mark_loose("<b>夜 の\nトイレ</b>", "夜のトイレ") == "&lt;b&gt;<mark>夜 の\nトイレ</mark>&lt;/b&gt;"


def test_mark_with_no_quote_only_escapes(env):
    assert mark_loose("<b>", "") == "&lt;b&gt;"


def test_opening_the_page_changes_no_records(env):
    s, c = env
    before = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    http, _ = client_for(s, said("答えです"))
    http.post("/desk/U04/ask", data={"question": "夜は？"})
    assert c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == before
