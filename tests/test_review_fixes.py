"""独立レビューで見つかった不具合の再発防止。"""
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.claude_checks import CheckRun, _BACKDATE, crosses_line, run_claude_checks, save_run
from keika.db import connect
from keika.drafts import WireAttendee, WireDraft, WireItem, WireSection, get_draft, list_drafts, make_draft, sync_review, update_draft
from keika.files import scan
from keika.findings import Evidence, Finding
from keika.intake import WireEntry, WireIntake, clean_transcript, invented_terms, open_reviews, original_of, process_text
from keika.ledger import User, seed_from_json, upsert_user
from keika.machine_checks import run_machine_checks
from keika.mcp_server import enabled, key_path, record_text
from keika.next_steps import WireDoc, WirePlan, WireStep, get_plan, make_plan
from keika.reading import DocFacts, _save
from keika.records import RecordIn, add_record, get_record, list_records
from keika.settings import Settings
from keika.web.app import create_app
from test_claude_checks import END, msg, report, tool
from test_claude_checks import Scripted
from test_reading import FakeClient, answer

TALK = "柚木様のお宅で14時から担当者会議を始めます。能登です。デイは週2回のまま続ける、これで決まりでいいですね。村上先生は欠席です。"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    (root / "入口").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def rec(c, kind="会議"):
    return add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read=kind, monitoring=False, body="会議を開いた。"))


def wire(items=(), **kw):
    data = dict(place="", start_time="", end_time="", attendees=[], sections=[WireSection(key="conclusion", items=list(items))] if items else [])
    return WireDraft(**(data | kw))


def make(s, c, w, text=TALK):
    return make_draft(c, FakeClient(answer(w)), s, rec(c), text, "会議.txt")


# --- 文字起こしの前処理で話を消さない ---


@pytest.mark.parametrize("text", [
    "9:00 田中さん宅を訪問\n10:30 デイに電話\n13:00 担当者会議",
    "（14:00）柚木様宅で担当者会議",
    "150 --> 130 に血圧が下がったと娘さん",
    "3 --> 2 に変わった",
    "話者1 10:30 に伺います",
])
def test_clean_transcript_keeps_spoken_content(text):
    out = clean_transcript(text)
    for piece in ("9:00", "10:30", "13:00", "14:00", "150", "130", "3 --> 2"):
        if piece in text:
            assert piece in out


def test_speech_line_just_before_a_cue_is_kept():
    assert "150" in clean_transcript("WEBVTT\n\n00:01.000 --> 00:02.000\n血圧は\n150\n00:02.000 --> 00:03.000\nでした")


def test_elapsed_stamps_starting_near_zero_are_removed():
    out = clean_transcript("00:00 はじめます\n00:29 こんにちは\n01:20 ふらつきは二回です\n")
    assert out.splitlines() == ["はじめます", "こんにちは", "ふらつきは二回です"]


def test_crlf_srt_numbers_are_removed_so_number_check_still_works():
    text = "1\r\n00:00:01,000 --> 00:00:02,000\r\nデイは週2回\r\n\r\n2\r\n00:00:02,000 --> 00:00:03,000\r\nです\r\n"
    out = clean_transcript(text)
    assert out.split() == ["デイは週2回", "です"]
    assert invented_terms(out, "デイは週1回") == ["1"]


def test_bom_vtt_header_is_removed():
    assert "WEBVTT" not in clean_transcript("﻿WEBVTT\n\n00:01.000 --> 00:02.000\nはい\n")


def test_original_text_is_kept_before_cleaning(env):
    s, c = env
    text = "1\n00:00:01,000 --> 00:00:02,000\n柚木さん宅を訪問。変わりなし。"
    e = WireEntry(user_id="U04", spoken_name="柚木さん", ambiguous=False, ambiguity_note="", event_date="2026-11-05", event_time="", kind_read="居宅訪問",
                  monitoring=False, partner="", body="柚木様宅を訪問した。変わりなし。", name_fixes=[], unsure_words=[])
    r = process_text(c, FakeClient(answer(WireIntake(entries=[e]))), s, text, "t")[0]
    assert "-->" in original_of(c, r.record.id)


# --- 下書き: 印と項目のずれ ---


def test_saving_unedited_form_keeps_flags_with_line_endings(env):
    s, c = env
    i = make(s, c, wire([WireItem(text="週5回に増やす。\n", quote=""), WireItem(text="二行目の文\nです。", quote="")]))
    items = get_draft(c, i).sections["conclusion"]
    rows = [{"id": items[0]["id"], "text": "週5回に増やす。\r\n"}, {"id": items[1]["id"], "text": "二行目の文\r\nです。"}]
    update_draft(c, i, place="", start_time="", end_time="", attendees=[], sections={"conclusion": rows})
    assert get_draft(c, i).flagged == 2


def test_stale_form_keeps_each_items_quote_and_flag(env):
    s, c = env
    i = make(s, c, wire([WireItem(text="案A。", quote=""), WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続ける")]))
    a, b = get_draft(c, i).sections["conclusion"]
    update_draft(c, i, place="", start_time="", end_time="", attendees=[], sections={"conclusion": [{"id": a["id"], "text": ""}, {"id": b["id"], "text": b["text"]}]})
    update_draft(c, i, place="", start_time="", end_time="", attendees=[], sections={"conclusion": [{"id": a["id"], "text": a["text"]}, {"id": b["id"], "text": b["text"]}]})
    x, y = get_draft(c, i).sections["conclusion"]
    assert x["flag"] and x["quote"] == "" and y["quote"] == "デイは週2回のまま続ける"


def test_each_sentence_form_carries_its_item_id(env):
    s, c = env
    i = make(s, c, wire([WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続ける")]))
    page = TestClient(create_app(s)).get(f"/drafts/{i}").text
    assert f'/drafts/{i}/items/{get_draft(c, i).sections["conclusion"][0]["id"]}/text' in page


# --- 下書き: 出席者、場所、時刻、医療 ---


def test_absent_person_is_not_listed_as_attendee(env):
    s, c = env
    d = get_draft(c, make(s, c, wire(attendees=[WireAttendee(org="", role="", name="村上", quote="村上先生は欠席です")])))
    assert d.attendees == []


def test_attendee_with_made_up_quote_becomes_blank(env):
    s, c = env
    d = get_draft(c, make(s, c, wire(attendees=[WireAttendee(org="", role="長男", name="柚木 健二", quote="作り話")])))
    assert d.attendees[0]["name"] == "〇〇" and d.attendees[0]["role"] == ""


def test_ledger_name_with_real_quote_is_kept(env):
    s, c = env
    w = wire(attendees=[WireAttendee(org="", role="妻", name="柚木 房江", quote="房江です")])
    d = get_draft(c, make(s, c, w, text=TALK + "房江です。"))
    assert d.attendees[0]["name"] == "柚木 房江"


def test_place_with_digit_not_in_talk_becomes_blank(env):
    s, c = env
    assert get_draft(c, make(s, c, wire(place="2階の会議室"))).place == "〇〇"


def test_time_needs_a_spoken_clock_not_any_digit(env):
    s, c = env
    d = get_draft(c, make(s, c, wire(start_time="14:00"), text="要介護2と14人の話です"))
    assert d.start_time == "〇〇"


def test_afternoon_clock_is_accepted(env):
    s, c = env
    assert get_draft(c, make(s, c, wire(start_time="14:00"), text="午後2時から始めました")).start_time == "14:00"


def test_held_does_not_keep_ai_sentence_or_fake_quote(env):
    s, c = env
    d = get_draft(c, make(s, c, wire([WireItem(text="降圧薬を中止すべきである。", quote="作り話の発言")])))
    assert d.held == [{"quote": ""}]
    assert "中止" not in TestClient(create_app(s)).get(f"/drafts/{d.id}").text


def test_held_keeps_real_quote_only(env):
    s, c = env
    d = get_draft(c, make(s, c, wire([WireItem(text="すぐ受診が必要である。", quote="能登です")])))
    assert d.held == [{"quote": "能登です"}]


def test_backdating_sentence_is_not_put_in_draft(env):
    s, c = env
    d = get_draft(c, make(s, c, wire([WireItem(text="署名日を前の日付に合わせる。", quote="能登です")])))
    assert d.sections["conclusion"][0]["text"] == "〇〇"


def test_short_unrelated_quote_gets_flag(env):
    s, c = env
    d = get_draft(c, make(s, c, wire([WireItem(text="長男が施設入所を希望した。", quote="です")])))
    assert d.sections["conclusion"][0]["flag"]


def test_medical_recommendation_phrase_crosses_the_line():
    assert crosses_line("かかりつけ医で診てもらうよう勧めた。")


# --- 下書きと確認待ち ---


def _pending_with_draft(s, c):
    e = WireEntry(user_id="U04", spoken_name="柚木様", ambiguous=False, ambiguity_note="", event_date="2026-11-05", event_time="", kind_read="会議",
                  monitoring=False, partner="", body="99人で会議を開いた。", name_fixes=[], unsure_words=[])
    w = wire([WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続ける")])
    from keika.drafts import intake_with_drafts
    intake_with_drafts(c, FakeClient(answer(WireIntake(entries=[e])), answer(w)), s, TALK, "会議.txt")
    return open_reviews(c)[0]["id"], list_drafts(c)[0].id


def test_setting_review_aside_removes_its_draft_from_list(env):
    s, c = env
    review, _ = _pending_with_draft(s, c)
    sync_review(c, review, None, None)
    assert list_drafts(c) == []


def test_choosing_another_user_moves_the_draft(env):
    s, c = env
    review, draft = _pending_with_draft(s, c)
    sync_review(c, review, "U01", None)
    assert get_draft(c, draft).user_id == "U01"


def test_user_page_links_checked_draft(env):
    s, c = env
    i = make(s, c, wire([WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続ける")]))
    client = TestClient(create_app(s))
    client.post(f"/drafts/{i}/checked")
    assert f"/drafts/{i}" in client.get("/users/U04").text


# --- 手順と文案 ---


def f_():
    return Finding(rule_id="R11", user_id="U04", kind="finding", period="2026-09", message="週3回のようです。確認してください。", todo="x",
                   evidence=[Evidence(file="支援経過（アプリの記録）", quote="週3回", record_date="2026-09-09")], source="claude")


def plan_of(s, c, w):
    return make_plan(c, FakeClient(answer(w)), s, f_())


@pytest.mark.parametrize("text", ["作成日をさかのぼって記入する", "署名日を2026-09-01に合わせて書き直す", "同意日を9月1日として書き直す"])
def test_backdating_step_is_removed(env, text):
    s, c = env
    made = plan_of(s, c, WirePlan(steps=[WireStep(text=text, who="ケアマネ"), WireStep(text="確かめる", who="ケアマネ")], docs=[]))
    assert [x["text"] for x in made["steps"]] == ["確かめる"]


def test_backdating_words():
    assert _BACKDATE.search("署名の日付を前の日付に合わせる。")
    assert not _BACKDATE.search("今日の日付で経緯を残す。")


def test_medical_title_removes_the_document(env):
    s, c = env
    made = plan_of(s, c, WirePlan(steps=[WireStep(text="確かめる", who="ケアマネ")], docs=[WireDoc(kind="その他の文", title="すぐ受診が必要な件", body="〇〇")]))
    assert made["docs"] == []


def test_unfilled_past_action_in_record_text_is_flagged(env):
    s, c = env
    made = plan_of(s, c, WirePlan(steps=[WireStep(text="確かめる", who="ケアマネ")], docs=[WireDoc(kind="支援経過の文", title="経緯", body="自宅を訪問し、本人と家族に説明して同意を得た。")]))
    assert "実際に" in made["docs"][0]["flag"]


def test_blank_action_is_not_flagged(env):
    s, c = env
    made = plan_of(s, c, WirePlan(steps=[WireStep(text="確かめる", who="ケアマネ")], docs=[WireDoc(kind="支援経過の文", title="経緯", body="〇〇に説明した。")]))
    assert made["docs"][0]["flag"] == ""


def test_empty_plan_is_not_saved(env):
    s, c = env
    assert plan_of(s, c, WirePlan(steps=[WireStep(text="すぐ受診が必要と伝える", who="")], docs=[])) is None
    assert get_plan(c, f_()) is None


def test_changed_finding_makes_a_new_plan(env):
    s, c = env
    plan_of(s, c, WirePlan(steps=[WireStep(text="確かめる", who="")], docs=[]))
    changed = f_().model_copy(update={"message": "別の内容のようです。確認してください。"})
    assert get_plan(c, changed) is None


def test_plan_page_before_making_says_not_yet(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[f_()]))
    from keika.findings import fingerprint
    page = TestClient(create_app(s)).get(f"/users/U04/plan/{fingerprint(f_())}").text
    assert "作れませんでした" not in page and "まだ作っていません" in page


# --- 確認待ちの訪問 ---


def _pending(c, user_id, day, warnings=()):
    entry = {"user_id": user_id, "kind_read": "居宅訪問", "event_date": day, "event_time": "", "monitoring": True, "partner": "", "body": "訪問した。"}
    c.execute("INSERT INTO review_items (original, source, entry, warnings, note, status, created_at) VALUES ('話', '口述.txt', ?, ?, '', 'open', '2026-11-04')",
              (json.dumps(entry, ensure_ascii=False), json.dumps(list(warnings), ensure_ascii=False)))
    c.commit()


def _visits(s, c):
    return [f for f in run_machine_checks(c, s, "U04") if f.rule_id == "R06"]


def test_review_whose_visit_is_unspoken_does_not_count(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-10-05", kind_read="居宅訪問", body="訪問。"))
    _pending(c, "U04", "2026-11-02", ["居宅訪問"])
    assert "まだ" in _visits(s, c)[0].message


def test_past_month_finding_keeps_its_message_and_evidence(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-08-05", kind_read="居宅訪問", body="訪問。"))
    add_record(c, RecordIn(user_id="U04", event_date="2026-09-20", kind_written="訪問", kind_read="電話", body="電話で聞いた。"))
    add_record(c, RecordIn(user_id="U04", event_date="2026-10-05", kind_read="居宅訪問", body="訪問。"))
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-01", kind_read="居宅訪問", body="訪問。"))
    _pending(c, "U04", "2026-09-22")
    found = _visits(s, c)[0]
    assert found.kind == "finding" and "電話" in found.message and found.evidence and "記録の確認" in found.message


def test_future_meeting_does_not_soften_missing_minutes(env):
    s, c = env
    upsert_user(c, User(id="U09", name="試験 太郎", cm="CM01", data={"type": "居宅介護支援"}))
    folder = s.drive_root / "利用者" / "U09_試験太郎"
    folder.mkdir()
    (folder / "計画書.txt").write_text("次回の担当者会議：2027-03-01", encoding="utf-8")
    ref = [r for r in scan(s) if r.rel_path.endswith("計画書.txt")][0]
    _save(c, ref, DocFacts(doc_type="居宅サービス計画書", summary="計画書"))
    found = [f for f in run_machine_checks(c, s, "U09") if f.rule_id == "R33" and "要点" in f.message]
    assert found[0].kind == "finding"


# --- 点検の周 ---


def _claude_env(tmp_path):
    root = tmp_path / "drive"
    folder = root / "利用者" / "U01_杉森ハル"
    folder.mkdir(parents=True)
    (folder / "計画書.txt").write_text("週2回 通所介護 を利用する。", encoding="utf-8")
    s = Settings(drive_root=root, db_path=tmp_path / "y.db", as_of=date(2026, 11, 4))
    c = connect(s)
    upsert_user(c, User(id="U01", name="杉森 ハル", cm="CM01", data={"type": "居宅介護支援", "cert_period": ["2026-04-01", "2028-03-31"]}))
    _save(c, scan(s)[0], DocFacts(doc_type="居宅サービス計画書", summary="計画書"))
    return s, c


def test_same_finding_in_second_look_is_not_added_twice(tmp_path):
    s, c = _claude_env(tmp_path)
    again = report(message="言い方を変えました。確認してください。")
    run = run_claude_checks(c, Scripted(msg(report()), END, msg(again), END), s, "U01")
    assert len(run.findings) == 1
    c.close()


def test_withdrawing_invalid_replace_keeps_real_replace(tmp_path):
    s, c = _claude_env(tmp_path)
    save_run(c, "U01", CheckRun(findings=[Finding(rule_id="R03", user_id="U01", kind="finding", period="2026-10", message="前回です。確認してください。", searched_scope="x")]))
    good = report(replaces="P1", period="2026-10")
    bad = report(rule_id="R02", replaces="P1", period="2026-09", todo="別の件")
    withdraw = tool("withdraw_finding", dict(number="N2", reason="誤り"))
    run = run_claude_checks(c, Scripted(msg(good), msg(bad), END, msg(withdraw), END), s, "U01")
    assert "P1" in run.replaced and not any(x.message == "前回です。確認してください。" for x in run.findings)
    c.close()


def test_empty_first_round_does_not_send_empty_assistant_message(tmp_path):
    s, c = _claude_env(tmp_path)
    empty = msg(stop_reason="end_turn")
    client = Scripted(empty)
    run_claude_checks(c, client, s, "U01")
    assert all(m.get("content") != [] for call in client.calls for m in call["messages"])
    c.close()


def test_second_look_failure_is_shown(env):
    s, c = env
    save_run(c, "U04", CheckRun(findings=[f_()], error="見直しは途中で止まりました: RuntimeError"))
    assert "見直しは途中で止まりました" in TestClient(create_app(s)).get("/users/U04").text


# --- 見本モードと鍵 ---


def test_sample_mode_refuses_every_post(env):
    s, c = env
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-02", kind_read="電話", body="もとの文。"))
    client = TestClient(create_app(s, client_factory=lambda: None, sample=True))
    data = dict(user_id="U04", event_date="2026-11-02", event_time="", kind_written="", partner="", body="書き換え", kind_read="", author="")
    assert client.post(f"/records/{r.id}/edit", data=data).status_code == 403
    assert client.post("/ledger", data={}).status_code == 403
    assert client.post("/office/R23/declare", data={}).status_code == 403
    assert get_record(c, r.id).body == "もとの文。"


def test_key_opens_only_its_own_database(tmp_path):
    a = Settings(drive_root=tmp_path, db_path=tmp_path / "a.db")
    b = Settings(drive_root=tmp_path, db_path=tmp_path / "b.db")
    key_path(a).touch()
    assert enabled(a) and not enabled(b)
    assert "止めて" in record_text(b, FakeClient(RuntimeError("呼ばれないはず")), "話")


# --- 点検表の更新案 ---


def _rule_tool():
    path = Path(__file__).parent.parent / "tools" / "rule_update" / "update_rules.py"
    spec = importlib.util.spec_from_file_location("update_rules_fix", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["update_rules_fix"] = mod
    spec.loader.exec_module(mod)
    return mod


NOTICE = "利用者の身体的拘束等を行う場合には、その態様及び時間を記録しなければならない。"
URL = "https://www.mhlw.go.jp/content/example.pdf"


def test_second_reader_with_empty_quote_goes_to_human():
    ur = _rule_tool()
    p = ur.WireProposal(title="t", plain="p", legal_basis="b", done_when="d", applies_to="both", effective_from="", quote="その態様及び時間を記録")
    routed = ur.route(FakeClient(answer(ur.WireVerdict(supported=True, quote="", reason=""))), NOTICE, URL, [p])
    assert routed.accepted == []


def test_impossible_date_is_dropped_and_rows_load():
    ur = _rule_tool()
    p = ur.WireProposal(title="t", plain="p", legal_basis="b", done_when="d", applies_to="both", effective_from="2027-02-30", quote="q")
    rows = ur.to_rows([p], [{"id": "R47"}], "通知", URL)
    assert "effective_from" not in rows[0]


def test_public_check_covers_subtitle_files(tmp_path):
    spec = importlib.util.spec_from_file_location("check_public_fix", Path(__file__).parent.parent / "tools" / "check_public.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "a.srt").write_text("電話は 090-" + "1234-5678 です", encoding="utf-8")
    assert mod.problems(["a.srt"], [], tmp_path)


def test_number_said_just_before_the_quote_is_not_flagged(env):
    s, c = env
    text = "10月の話です。30日の夜中に尻もちをつきました。" + "あ" * 300 + "週に5回は来ています。"
    w = wire([WireItem(text="10月30日の夜中に尻もちをついた。", quote="30日の夜中に尻もちをつきました")])
    assert get_draft(c, make(s, c, w, text=text)).sections["conclusion"][0]["flag"] == ""


def test_number_said_far_from_the_quote_is_flagged(env):
    s, c = env
    text = "週に5回は来ています。" + "あ" * 300 + "デイは週2回のまま続けます。"
    w = wire([WireItem(text="デイは週5回のまま続ける。", quote="デイは週2回のまま続けます")])
    assert "5" in get_draft(c, make(s, c, w, text=text)).sections["conclusion"][0]["flag"]


def test_medicine_warning_only_for_sentences_about_medicine():
    original = "薬は飲み忘れがありました。部屋は片付いていました。"
    assert invented_terms(original, "部屋は片付いていた。") == []
    assert invented_terms(original, "薬を飲めている。") == ["飲めている"]


def test_name_spoken_in_kana_is_not_flagged_in_draft(env):
    s, c = env
    text = "柚木さんのお宅です。ふさえさんはいなかったです。デイは週2回です。"
    w = wire([WireItem(text="柚木房江さんは不在だった。", quote="ふさえさんはいなかったです")])
    assert get_draft(c, make(s, c, w, text=text)).sections["conclusion"][0]["flag"] == ""


def test_name_never_spoken_is_still_flagged_in_draft(env):
    s, c = env
    text = "柚木さんのお宅です。奥さんはいなかったです。デイは週2回です。"
    w = wire([WireItem(text="柚木房江さんは不在だった。", quote="奥さんはいなかったです")])
    assert "房江" in get_draft(c, make(s, c, w, text=text)).sections["conclusion"][0]["flag"]
