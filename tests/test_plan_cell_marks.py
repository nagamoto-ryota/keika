"""ケアプランとアセスメントの下書きのマス: 元の書類は押した時だけ出す、見比べたい語だけ赤くする、
直すか〔これでいい〕で赤が消える（履歴に残る）、日付の書き方違いや元にある語のつながりは赤くしない。"""
import json
import re

from fastapi.testclient import TestClient

from keika.care_plan import COMPARE, BLANK, WireItem, WirePlan, WireSection, edit_cell, edit_history, get_care_plan, make_care_plan
from keika.claude_checks import APP_RECORDS
from keika.records import RecordIn, add_record
from keika.web.app import create_app
from test_care_plan import ASSESS, env  # noqa: F401  （env は fixture）
from test_reading import FakeClient, answer

RECORD = "夜間のトイレは毎回恵子さんが起きて付き添っている。次女は近くに住む。"


def plan_with(s, c, *items):
    add_record(c, RecordIn(user_id="U01", event_date="2026-11-02", kind_read="居宅訪問", body=RECORD))
    wire = WirePlan(sections=[WireSection(key=k, items=[WireItem(text=t, who="", file=f, quote=q)]) for k, t, q, f in items], rows=[], week=[])
    pid = make_care_plan(c, FakeClient(answer(wire)), s, "U01", kind="assessment")
    return get_care_plan(c, pid)


def cell(p, key):
    return p.sections[key][0]


def test_date_written_differently_is_not_marked(env):
    s, c = env
    p = plan_with(s, c, ("a1", "2026-11-02に居宅訪問し、付き添いの話を聞いた。", "毎回恵子さんが起きて付き添っている", APP_RECORDS),
                  ("a2", "11/2の訪問で、夜間のトイレは毎回付き添うと聞いた。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    assert cell(p, "a1")["flag"] == "" and cell(p, "a2")["flag"] == ""


def test_words_joined_from_the_source_are_not_marked_but_new_ones_are(env):
    s, c = env
    p = plan_with(s, c, ("a16", "夜間のトイレは毎回次女が付き添う。", "毎回恵子さんが起きて付き添っている", APP_RECORDS),
                  ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    assert cell(p, "a16")["flag"] == "" and cell(p, "a16")["words"] == []
    assert "入院" in cell(p, "a10")["words"] and cell(p, "a10")["flag"]


def page(s, p):
    return TestClient(create_app(s)).get(f"/careplans/{p.id}").text


def test_source_is_hidden_until_the_sentence_is_pressed(env):
    s, c = env
    p = plan_with(s, c, ("a11", "階段は手すりを使う", "階段は手すりを使う", ASSESS))
    html = page(s, p)
    box = html.split(f'id="cell-{cell(p, "a11")["id"]}"')[1].split("</details>")[0]
    assert '<details class="cell-more">' in html.split(f'id="cell-{cell(p, "a11")["id"]}"')[1][:400]
    assert box.index("<summary") < box.index("元の書類") and "<details class=\"cell-more\" open" not in html
    assert "▸ 元の書類" not in html and "<summary class=\"muted\">元の書類</summary>" not in html


def test_only_the_words_to_compare_are_red_and_the_long_note_is_gone(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    html = page(s, p)
    assert '<mark class="cmp">入院</mark>' in html
    assert COMPARE not in html


def test_editing_the_cell_clears_the_red_and_the_count(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    before = p.flagged
    edit_cell(c, p.id, cell(p, "a10")["id"], "夜間のトイレは毎回付き添う。")
    q = get_care_plan(c, p.id)
    assert cell(q, "a10")["flag"] == "" and cell(q, "a10")["words"] == [] and q.flagged == before - 1


def test_this_is_fine_clears_the_red_and_stays_in_the_history(env):
    s, c = env
    p = plan_with(s, c, ("a10", "夜間のトイレで入院した。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    client = TestClient(create_app(s))
    assert ">これでいい</button>" in client.get(f"/careplans/{p.id}").text
    r = client.post(f"/careplans/{p.id}/ok", data={"item": cell(p, "a10")["id"]}, follow_redirects=False)
    assert r.status_code == 303
    q = get_care_plan(c, p.id)
    assert cell(q, "a10")["flag"] == "" and cell(q, "a10")["text"] == "夜間のトイレで入院した。" and q.flagged == p.flagged - 1
    assert any("これでいい" in h["after_text"] for h in edit_history(c, p.id))


def test_old_drafts_are_looked_at_again_with_the_new_rule_once(env):
    s, c = env
    p = plan_with(s, c, ("a1", "2026-11-02に居宅訪問し、付き添いの話を聞いた。", "毎回恵子さんが起きて付き添っている", APP_RECORDS))
    row = c.execute("SELECT payload FROM care_plans WHERE id = ?", (p.id,)).fetchone()
    d = json.loads(row["payload"])
    target = d["sections"]["a1"][0]
    target["flag"] = f"{COMPARE}02、11、2026"
    target.pop("words", None)
    d.pop("marks_v", None)
    c.execute("UPDATE care_plans SET payload = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), p.id))
    c.commit()
    assert get_care_plan(c, p.id).flagged == 1
    page(s, p)
    assert get_care_plan(c, p.id).flagged == 0


def test_blank_cells_stay_plain_and_red(env):
    s, c = env
    p = plan_with(s, c, ("a11", "階段は手すりを使う", "階段は手すりを使う", ASSESS))
    html = page(s, p)
    blank_id = cell(p, "a12")["id"]
    assert cell(p, "a12")["text"] == BLANK
    part = html.split(f'id="cell-{blank_id}"')[0].rsplit("<div", 1)[1]
    assert "blank-cell" in part
