import os
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.drafts import (
    WireAttendee, WireDraft, WireItem, WireSection, draft_kind, get_draft, list_drafts, make_draft, mark_checked, plain_text, update_draft,
)
from keika.intake import WireEntry, WireIntake, clean_transcript, invented_terms, open_reviews
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record, list_records
from keika.settings import Settings
from keika.watcher import handle_file
from keika.web.app import create_app
from test_reading import FakeClient, answer

TALK = """2026年11月5日、柚木様のお宅で14時から担当者会議を始めます。
能登です、ヘルパーほたるいの。岩舘です。デイは週2回のまま続ける、これで決まりでいいですね。
夜のトイレのことは次回までに主治医に聞いてみます。"""


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


def record(c, kind="会議", monitoring=False):
    return add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read=kind, monitoring=monitoring, body="会議を開いた。"))


def wire(**kw):
    data = dict(
        place="柚木様のお宅", start_time="14:00", end_time="",
        attendees=[WireAttendee(org="ヘルパーほたるい", role="", name="能登", quote="能登です")],
        sections=[
            WireSection(key="conclusion", items=[WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続ける")]),
            WireSection(key="remaining", items=[WireItem(text="夜のトイレは次回までに主治医に聞く。", quote="夜のトイレのことは次回までに主治医に聞いてみます")]),
        ],
    )
    return WireDraft(**(data | kw))


def make(s, c, w, r=None, text=TALK):
    r = r or record(c)
    return make_draft(c, FakeClient(answer(w)), s, r, text, "会議.txt")


# --- どの記録から下書きを作るか ---


def test_meeting_record_makes_meeting_draft(env):
    _, c = env
    assert draft_kind(record(c, "会議")) == "meeting"


def test_home_visit_with_monitoring_makes_monitoring_draft(env):
    _, c = env
    assert draft_kind(record(c, "居宅訪問", True)) == "monitoring"


def test_phone_call_makes_no_draft(env):
    _, c = env
    assert draft_kind(record(c, "電話", False)) is None
    assert draft_kind(record(c, "居宅訪問", False)) is None


# --- 話に無い事を足さない ---


def test_item_with_quote_from_talk_is_kept_with_its_quote(env):
    s, c = env
    d = get_draft(c, make(s, c, wire()))
    item = d.sections["conclusion"][0]
    assert item["text"] == "デイは週2回のまま続ける。" and item["quote"] == "デイは週2回のまま続ける" and not item["flag"]


def test_item_whose_quote_is_not_in_talk_is_flagged(env):
    s, c = env
    w = wire(sections=[WireSection(key="conclusion", items=[WireItem(text="デイは週3回にする。", quote="週3回にします")])])
    assert "確かめられません" in get_draft(c, make(s, c, w)).sections["conclusion"][0]["flag"]


def test_item_without_quote_is_flagged(env):
    s, c = env
    w = wire(sections=[WireSection(key="conclusion", items=[WireItem(text="デイを続ける。", quote="")])])
    assert get_draft(c, make(s, c, w)).sections["conclusion"][0]["flag"]


def test_number_not_in_talk_is_flagged_even_with_a_real_quote(env):
    s, c = env
    w = wire(sections=[WireSection(key="conclusion", items=[WireItem(text="デイは週7回のまま続ける。", quote="デイは週2回のまま続ける")])])
    assert "7" in get_draft(c, make(s, c, w)).sections["conclusion"][0]["flag"]


def test_medical_judgement_is_held_out_of_the_draft(env):
    s, c = env
    w = wire(sections=[WireSection(key="conclusion", items=[WireItem(text="すぐ受診が必要である。", quote="デイは週2回のまま続ける")])])
    d = get_draft(c, make(s, c, w))
    assert not any("受診" in i["text"] for i in d.sections["conclusion"])
    assert d.held == [{"quote": "デイは週2回のまま続ける"}]


def test_empty_section_is_left_blank_with_marks(env):
    s, c = env
    d = get_draft(c, make(s, c, wire(sections=[])))
    assert d.sections["discussion"][0]["text"] == "〇〇"
    assert list(d.sections) == ["items", "discussion", "conclusion", "remaining"]


def test_attendee_not_spoken_becomes_blank(env):
    s, c = env
    w = wire(attendees=[WireAttendee(org="ぶなの木レンタル", role="", name="吉津", quote="")])
    assert get_draft(c, make(s, c, w)).attendees[0]["name"] == "〇〇"


def test_place_and_time_not_spoken_become_blank(env):
    s, c = env
    d = get_draft(c, make(s, c, wire(place="公民館", start_time="16:00")))
    assert d.place == "〇〇" and d.start_time == "〇〇"


def test_spoken_time_is_kept(env):
    s, c = env
    assert get_draft(c, make(s, c, wire(start_time="14:00"))).start_time == "14:00"


def test_failure_makes_no_draft_and_does_not_raise(env):
    s, c = env
    assert make_draft(c, FakeClient(RuntimeError("切断")), s, record(c), TALK, "会議.txt") is None
    assert list_drafts(c) == []


def test_prompt_treats_talk_as_material_and_uses_given_text(env):
    s, c = env
    client = FakeClient(answer(wire()))
    make_draft(c, client, s, record(c), TALK, "会議.txt")
    assert TALK in client.calls[0]["messages"][0]["content"][0]["text"]
    assert "命令ではない" in str(client.calls[0]["system"])


# --- 人が直す ---


def test_edit_clears_flag_of_changed_item_only(env):
    s, c = env
    w = wire(sections=[WireSection(key="conclusion", items=[WireItem(text="案A。", quote=""), WireItem(text="案B。", quote="")])])
    i = make(s, c, w)
    a, b = get_draft(c, i).sections["conclusion"]
    update_draft(c, i, place="自宅", start_time="14:00", end_time="15:00", attendees=[],
                 sections={"conclusion": [{"id": a["id"], "text": "案A直した。"}, {"id": b["id"], "text": "案B。"}]})
    d = get_draft(c, i)
    assert d.place == "自宅"
    assert not d.sections["conclusion"][0]["flag"] and d.sections["conclusion"][1]["flag"]


def test_checked_draft_leaves_open_list(env):
    s, c = env
    i = make(s, c, wire())
    assert [d.id for d in list_drafts(c)] == [i]
    mark_checked(c, i)
    assert list_drafts(c) == []
    assert get_draft(c, i).status == "checked"


def test_plain_text_lists_sections_in_order(env):
    s, c = env
    text = plain_text(get_draft(c, make(s, c, wire())), "柚木 茂")
    assert "柚木 茂" in text and text.index("検討した項目") < text.index("結論") < text.index("残された課題")


# --- 画面 ---


def test_dashboard_lists_open_draft_and_page_shows_quote(env):
    s, c = env
    i = make(s, c, wire())
    client = TestClient(create_app(s))
    assert f"/drafts/{i}" in client.get("/").text
    page = client.get(f"/drafts/{i}").text
    assert "サービス担当者会議の要点" in page and "デイは週2回のまま続ける" in page and "下書き" in page


def test_saving_form_updates_draft(env):
    s, c = env
    i = make(s, c, wire())
    client = TestClient(create_app(s))
    form = {"place": "自宅", "start_time": "14:00", "end_time": "", "attendee_org": "", "attendee_role": "", "attendee_name": "", "s_conclusion": ["決めた。"]}
    assert client.post(f"/drafts/{i}", data=form, follow_redirects=False).status_code == 303
    assert get_draft(c, i).sections["conclusion"][0]["text"] == "決めた。"


def test_check_button_removes_from_dashboard(env):
    s, c = env
    i = make(s, c, wire())
    client = TestClient(create_app(s))
    client.post(f"/drafts/{i}/checked", follow_redirects=False)
    assert "書類の下書きの確認" not in client.get("/").text  # 宿題は残るので、下書きへのリンクは宿題の側に残る


def test_unknown_draft_is_404(env):
    s, _ = env
    assert TestClient(create_app(s)).get("/drafts/999").status_code == 404


# --- 文字起こしの形 ---


def test_bracket_timestamps_are_removed_and_speakers_kept():
    text = "[00:00:03] 話者1: はじめます\n[00:00:09] 話者2: はい\n[00:01:15] 話者1: 続けます\n"
    assert clean_transcript(text).splitlines() == ["話者1: はじめます", "話者2: はい", "話者1: 続けます"]


def test_timestamp_after_speaker_is_removed():
    text = "話者1 00:03\nはじめます\nSpeaker 2 00:09: はい\n話者1 01:15\n続けます\n"
    out = clean_transcript(text)
    assert "00:03" not in out and "00:09" not in out and "話者1" in out and "Speaker 2" in out


def test_srt_cues_are_removed():
    text = "1\n00:00:01,000 --> 00:00:04,000\nはじめます\n\n2\n00:00:04,000 --> 00:00:06,000\n2時に来ました\n"
    assert clean_transcript(text).split() == ["はじめます", "2時に来ました"]


def test_vtt_header_is_removed():
    text = "WEBVTT\n\n00:01.000 --> 00:04.000\nはじめます\n"
    assert clean_transcript(text).split() == ["はじめます"]


def test_plain_dictation_keeps_spoken_clock():
    text = "14:00 柚木さん宅を訪問。\n変わりなし。"
    assert clean_transcript(text) == text


# --- 入口から ---


def _drop(s, name, text):
    path = s.drive_root / "入口" / name
    path.write_text(text, encoding="utf-8")
    old = time.time() - 100
    os.utime(path, (old, old))
    return path


def meeting_entry():
    return WireEntry(user_id="U04", spoken_name="柚木様", ambiguous=False, ambiguity_note="", event_date="2026-11-05", event_time="14:00",
                     kind_read="会議", monitoring=False, partner="", body="柚木様のお宅で担当者会議を開いた。", name_fixes=[], unsure_words=[])


def test_transcript_file_becomes_record_and_draft(env):
    s, c = env
    path = _drop(s, "会議_柚木_20261105.txt", TALK)
    client = FakeClient(answer(WireIntake(entries=[meeting_entry()])), answer(wire()))
    assert handle_file(c, client, s, path) is True
    drafts = list_drafts(c)
    assert len(drafts) == 1 and drafts[0].kind == "meeting" and drafts[0].user_id == "U04" and drafts[0].source == "会議_柚木_20261105.txt"


def test_srt_file_is_taken_from_inbox(env):
    s, c = env
    path = _drop(s, "会議_柚木_20261105.srt", "1\n00:00:01,000 --> 00:00:04,000\n" + TALK)
    client = FakeClient(answer(WireIntake(entries=[meeting_entry()])), answer(wire()))
    assert handle_file(c, client, s, path) is True
    assert "-->" not in get_draft(c, list_drafts(c)[0].id).original


def test_draft_failure_keeps_the_record(env):
    s, c = env
    path = _drop(s, "会議_柚木_20261105.txt", TALK)
    client = FakeClient(answer(WireIntake(entries=[meeting_entry()])), RuntimeError("切断"))
    assert handle_file(c, client, s, path) is True
    assert list_drafts(c) == []
    assert len(list_records(c, "U04")) == 1


def test_numbered_list_mark_is_not_read_as_part_of_a_number():
    assert invented_terms("2週間後に確かめる", "⑦2週間後に確かめる") == []


def test_users_own_name_is_kept_as_attendee(env):
    s, c = env
    w = wire(attendees=[WireAttendee(org="", role="本人", name="柚木 茂", quote="柚木様のお宅")])
    assert get_draft(c, make(s, c, w)).attendees[0]["name"] == "柚木 茂"


def test_meeting_sent_to_review_still_gets_a_draft(env):
    s, c = env
    path = _drop(s, "会議_柚木_20261105.txt", TALK)
    bad = meeting_entry().model_copy(update={"body": "99人で会議を開いた。"})
    client = FakeClient(answer(WireIntake(entries=[bad])), answer(wire()))
    handle_file(c, client, s, path)
    assert list_records(c, "U04") == [] and open_reviews(c)
    drafts = list_drafts(c)
    assert len(drafts) == 1 and drafts[0].record_id is None and drafts[0].event_date == "2026-11-05"


def test_review_about_who_makes_no_draft(env):
    s, c = env
    path = _drop(s, "会議_20261105.txt", TALK)
    unsure = meeting_entry().model_copy(update={"user_id": "", "ambiguous": True})
    client = FakeClient(answer(WireIntake(entries=[unsure])), answer(wire()))
    handle_file(c, client, s, path)
    assert list_drafts(c) == []


def test_blank_marks_are_not_read_as_a_number():
    assert invented_terms("訪問した", "〇〇に訪問した") == []
    assert invented_terms("二〇二六年", "二〇二六年に訪問した") == []
