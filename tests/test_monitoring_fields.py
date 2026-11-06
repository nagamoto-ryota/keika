"""モニタリングの下書きに、様式に必ずある「満足度」と「目標の達成状況」を足す（11/6 試用の声）。

満足度は文字起こしの言葉だけを根拠にし、話に無ければ推測で埋めない。
目標の達成状況は、その人のケアプランの目標ごとに 達成 / 一部達成 / 未達成 / 判断できない と根拠の一言を書く。
"""
import io
import json
from datetime import date

import docx
import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.drafts import NOT_DISCUSSED, NO_PLAN_GOALS, WireAchievement, WireDraft, WireItem, WireSection, get_draft, make_draft, plain_text, to_docx
from keika.files import scan
from keika.ledger import seed_from_json
from keika.reading import DocFacts, TextFact, _save
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer

TALK = """2026年11月5日、柚木様のお宅に訪問しました。
本人: デイは楽しいよ、行くのが待ち遠しい。
長女: 最近は庭まで杖で出られる日が増えました。でも雨の日は無理ですね。
次回はリハビリの様子を事業所に聞いてみます。"""

GOALS = ["自分で歩いて庭に出る", "週2回デイに通い人と話す"]


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "U04_柚木茂").mkdir(parents=True)
    (root / "入口").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def with_plan(s, c, goals=GOALS):
    path = s.drive_root / "利用者" / "U04_柚木茂" / "計画書.txt"
    path.write_text("計画書", encoding="utf-8")
    ref = next(r for r in scan(s) if r.rel_path.endswith("計画書.txt"))
    _save(c, ref, DocFacts(doc_type="居宅サービス計画書", summary="計画書", user_name="柚木 茂", period_start="2026-06-01", period_end="2026-11-30",
                           goals=[TextFact(text=g) for g in goals]))


def visit(c):
    return add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="居宅訪問", monitoring=True, body="訪問した。"))


def wire(satisfaction=None, achievements=None, **kw):
    sections = [WireSection(key="condition", items=[WireItem(text="庭まで杖で出られる日が増えた。", quote="最近は庭まで杖で出られる日が増えました")])]
    if satisfaction is not None:
        sections.append(WireSection(key="satisfaction", items=satisfaction))
    data = dict(place="柚木様のお宅", start_time="", end_time="", attendees=[], sections=sections, achievements=achievements or [])
    return WireDraft(**(data | kw))


def make(s, c, w, text=TALK):
    client = FakeClient(answer(w))
    draft_id = make_draft(c, client, s, visit(c), text, "訪問.txt")
    return get_draft(c, draft_id), client


def test_monitoring_has_satisfaction_and_achievement_in_the_form_order(env):
    s, c = env
    d, _ = make(s, c, wire())
    keys = list(d.headings)
    assert d.headings["satisfaction"] == "満足度" and d.headings["achievement"] == "目標の達成状況"
    assert keys.index("services") < keys.index("satisfaction") < keys.index("goals") < keys.index("achievement") < keys.index("plan_change")


def test_meeting_draft_does_not_get_the_new_fields(env):
    s, c = env
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="会議", body="会議を開いた。"))
    d = get_draft(c, make_draft(c, FakeClient(answer(wire())), s, r, TALK, "会議.txt"))
    assert "satisfaction" not in d.headings and "achievement" not in d.headings


def test_satisfaction_said_in_the_talk_is_kept_with_its_words(env):
    s, c = env
    d, _ = make(s, c, wire(satisfaction=[WireItem(text="本人はデイを楽しみにしている。", quote="デイは楽しいよ、行くのが待ち遠しい")]))
    item = d.sections["satisfaction"][0]
    assert item["text"] == "本人はデイを楽しみにしている。" and item["quote"] == "デイは楽しいよ、行くのが待ち遠しい" and not item["flag"]


def test_satisfaction_not_in_the_talk_is_not_guessed(env):
    s, c = env
    d, _ = make(s, c, wire(satisfaction=[]))
    assert [i["text"] for i in d.sections["satisfaction"]] == [NOT_DISCUSSED]
    assert NOT_DISCUSSED == "話に出ていない（次回確かめる）"


def test_satisfaction_without_real_words_is_flagged(env):
    s, c = env
    d, _ = make(s, c, wire(satisfaction=[WireItem(text="家族はサービスに満足している。", quote="家族はとても満足しています")]))
    assert d.sections["satisfaction"][0]["flag"]


def test_each_plan_goal_gets_a_status_and_reason(env):
    s, c = env
    with_plan(s, c)
    d, _ = make(s, c, wire(achievements=[
        WireAchievement(goal_no=1, status="一部達成", reason="杖で庭まで出られる日が増えたが、雨の日は出られない", quote="最近は庭まで杖で出られる日が増えました"),
        WireAchievement(goal_no=2, status="達成", reason="デイを楽しみに通っている", quote="デイは楽しいよ、行くのが待ち遠しい"),
    ]))
    texts = [i["text"] for i in d.sections["achievement"]]
    assert texts == ["自分で歩いて庭に出る: 一部達成（杖で庭まで出られる日が増えたが、雨の日は出られない）", "週2回デイに通い人と話す: 達成（デイを楽しみに通っている）"]
    assert d.sections["achievement"][0]["quote"] == "最近は庭まで杖で出られる日が増えました"


def test_the_plan_goals_are_given_to_claude(env):
    s, c = env
    with_plan(s, c)
    _, client = make(s, c, wire())
    prompt = json.dumps(client.calls[0], ensure_ascii=False, default=str)
    assert "1. 自分で歩いて庭に出る" in prompt and "2. 週2回デイに通い人と話す" in prompt


def test_a_goal_claude_skipped_is_left_as_cannot_judge(env):
    s, c = env
    with_plan(s, c)
    d, _ = make(s, c, wire(achievements=[WireAchievement(goal_no=1, status="一部達成", reason="庭まで出られる日が増えた", quote="最近は庭まで杖で出られる日が増えました")]))
    assert d.sections["achievement"][1]["text"] == "週2回デイに通い人と話す: 判断できない（話に出ていない）"


def test_a_status_without_real_words_is_flagged(env):
    s, c = env
    with_plan(s, c)
    d, _ = make(s, c, wire(achievements=[WireAchievement(goal_no=1, status="達成", reason="一人で庭に出られた", quote="一人で庭に出られました")]))
    assert d.sections["achievement"][0]["flag"]


def test_cannot_judge_needs_no_words(env):
    s, c = env
    with_plan(s, c)
    d, _ = make(s, c, wire(achievements=[WireAchievement(goal_no=2, status="判断できない", reason="話に出ていない", quote="")]))
    assert d.sections["achievement"][1] == {**d.sections["achievement"][1], "text": "週2回デイに通い人と話す: 判断できない（話に出ていない）", "flag": ""}


def test_a_goal_number_not_in_the_plan_is_ignored(env):
    s, c = env
    with_plan(s, c)
    d, _ = make(s, c, wire(achievements=[WireAchievement(goal_no=7, status="達成", reason="できた", quote="デイは楽しいよ、行くのが待ち遠しい")]))
    assert len(d.sections["achievement"]) == 2 and all("判断できない" in i["text"] for i in d.sections["achievement"])


def test_without_a_plan_the_status_is_cannot_judge(env):
    s, c = env
    d, _ = make(s, c, wire(achievements=[WireAchievement(goal_no=1, status="達成", reason="できた", quote="デイは楽しいよ、行くのが待ち遠しい")]))
    assert [i["text"] for i in d.sections["achievement"]] == [NO_PLAN_GOALS]
    assert "判断できない" in NO_PLAN_GOALS


def test_new_fields_show_on_the_page_with_the_ok_button_and_in_word(env):
    s, c = env
    with_plan(s, c)
    d, _ = make(s, c, wire(satisfaction=[], achievements=[WireAchievement(goal_no=1, status="達成", reason="一人で庭に出られた", quote="一人で庭に出られました")]))
    html = TestClient(create_app(s)).get(f"/drafts/{d.id}").text
    part = html.split('id="sec-achievement"')[1].split("<h2")[0]
    assert "to-check" in part and "いいよ</button>" in part
    assert NOT_DISCUSSED in html.split('id="sec-satisfaction"')[1].split("<h2")[0]
    words = "\n".join(p.text for p in docx.Document(io.BytesIO(to_docx(d, "柚木 茂", ""))).paragraphs)
    assert "満足度" in words and "目標の達成状況" in words and "【確かめる】自分で歩いて庭に出る: 達成" in words
    assert "【満足度】" in plain_text(d, "柚木 茂")


def test_ok_on_a_new_field_clears_its_mark(env):
    s, c = env
    with_plan(s, c)
    d, _ = make(s, c, wire(achievements=[WireAchievement(goal_no=1, status="達成", reason="一人で庭に出られた", quote="一人で庭に出られました")]))
    item = d.sections["achievement"][0]
    TestClient(create_app(s)).post(f"/drafts/{d.id}/items/{item['id']}/ok")
    assert not get_draft(c, d.id).sections["achievement"][0]["flag"]


def test_an_older_draft_without_the_new_fields_still_opens_and_saves_to_word(env):
    s, c = env
    d, _ = make(s, c, wire())
    row = c.execute("SELECT payload FROM drafts WHERE id = ?", (d.id,)).fetchone()
    data = json.loads(row["payload"])
    for key in ("satisfaction", "achievement"):
        data["sections"].pop(key)
    c.execute("UPDATE drafts SET payload = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), d.id))
    c.commit()
    web = TestClient(create_app(s))
    assert web.get(f"/drafts/{d.id}").status_code == 200
    assert web.get(f"/drafts/{d.id}/docx").status_code == 200
