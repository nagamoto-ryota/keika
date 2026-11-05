"""下書き画面: 先頭の指示1行、本文の中で直す、残りの数は1か所。"""
import re

from fastapi.testclient import TestClient

from keika.draft_card import do_line, draft_view, keep_blank, ok_item, settle_draft
from keika.drafts import get_draft
from keika.web.app import create_app
from test_draft_card import draft, env, first, page  # noqa: F401
from test_settle_flags import MED, verdicts


def clear(c, i):
    for items in get_draft(c, i).sections.values():
        for item in items:
            if item["flag"]:
                ok_item(c, i, item["id"])


def set_blanks_aside(c, i):
    for b in draft_view(c, get_draft(c, i))["blanks"]:
        keep_blank(c, i, b["field"])


def blank_input(html, name, cls="is-blank"):
    return re.search(rf'<input[^>]*class="[^"]*{cls}[^"]*"[^>]*name="{name}"[^>]*value="〇〇"', html)


def test_do_line_follows_what_is_left():
    one = [{}]
    assert do_line({"blanks": one, "checks": [], "med_list": []}) == "赤い所を埋めてください"
    assert do_line({"blanks": [], "checks": one, "med_list": []}) == "黄色の文を確かめてください"
    assert do_line({"blanks": [], "checks": [], "med_list": one}) == "黄色の薬の文を見てください"
    assert do_line({"blanks": one, "checks": one, "med_list": []}) == "赤と黄色の所を直してください"
    assert do_line({"blanks": one, "checks": [], "med_list": one}) == "赤と黄色の所を直してください"
    assert do_line({"blanks": [], "checks": [], "med_list": []}) == "Word で保存してください"


def test_top_is_one_instruction_without_the_file_name_or_notice(env):
    s, c = env
    i = draft(s, c)
    html = page(s, i)
    assert '<h1 class="do-line">赤と黄色の所を直してください</h1>' in html
    assert "AI が作った下書きです" not in html
    assert html.index("do-line") < html.index("doc-sheet")
    at = html.index("draft-word")
    assert "最後に決めるのは人です" in html[at - 300:at + 300]  # 保存ボタンの近くに短く残す


def test_blank_field_is_red_and_can_be_typed_in_place(env):
    s, c = env
    html = page(s, draft(s, c))
    assert blank_input(html, "end_time") and blank_input(html, "place")
    assert 'is-blank" name="start_time"' not in html  # 話にあった時間は赤くしない


def test_head_is_not_folded(env):
    s, c = env
    html = page(s, draft(s, c))
    assert "場所、時間、出席者を直す" not in html and "head-edit" not in html
    assert 'name="start_time"' in html and 'name="attendee_name"' in html


def test_typing_a_time_in_place_saves_it_in_the_usual_form(env):
    s, c = env
    i = draft(s, c)
    form = {"place": "〇〇", "start_time": "14:00", "end_time": "15時半", "attendee_org": "", "attendee_role": "", "attendee_name": ""}
    assert TestClient(create_app(s)).post(f"/drafts/{i}", data=form, follow_redirects=False).status_code == 303
    d = get_draft(c, i)
    assert d.end_time == "15:30" and d.place == "〇〇"
    assert "end_time" not in {b["field"] for b in draft_view(c, d)["blanks"]}


def test_attendee_known_only_by_blank_name_survives_saving(env):
    """〇〇 の欄をそのまま送っても、出席者の行は消えない。"""
    s, c = env
    i = draft(s, c)
    form = {"place": "〇〇", "start_time": "14:00", "end_time": "〇〇", "attendee_org": ["", ""], "attendee_role": ["", ""], "attendee_name": ["〇〇", ""]}
    TestClient(create_app(s)).post(f"/drafts/{i}", data=form)
    assert [a["name"] for a in get_draft(c, i).attendees] == ["〇〇"]


def test_blank_can_be_set_aside_in_place(env):
    s, c = env
    i = draft(s, c)
    html = page(s, i)
    assert 'name="field" value="end_time"' in html and "分からない" in html
    r = TestClient(create_app(s)).post(f"/drafts/{i}/blank", data={"field": "end_time", "action": "keep"}, follow_redirects=False)
    assert r.status_code == 303
    html = page(s, i)
    assert not blank_input(html, "end_time")
    assert blank_input(html, "end_time", "is-kept")  # 無くさず、〇〇 は見える


def test_empty_section_is_typed_in_place(env):
    s, c = env
    i = draft(s, c)
    html = page(s, i)
    assert re.search(r'<textarea[^>]*class="[^"]*is-blank[^"]*"[^>]*name="answer"', html)
    assert 'name="field" value="s:condition"' in html
    TestClient(create_app(s)).post(f"/drafts/{i}/blank", data={"field": "s:condition", "answer": "落ち着いていた。"})
    assert get_draft(c, i).sections["condition"][0]["text"] == "落ち着いていた。"


def _item_part(html, item_id):
    at = html.index(f'id="item-{item_id}"')
    return html[at - 60:html.index("</details>\n  <!-- /item -->", at)]


def test_check_is_in_the_body_with_what_was_said_and_buttons(env):
    s, c = env
    i = draft(s, c)
    item = first(c, i)
    html = page(s, i)
    assert "draft-check" not in html and "文字起こしにはこうあります" not in html
    part = _item_part(html, item["id"])
    assert "to-check" in part and item["text"] in part and item["quote"] in part
    assert ">いいよ</button>" in part and "<summary>違う</summary>" in part


def test_ok_with_edited_text_keeps_the_edit(env):
    """直してから〔いいよ〕を押しても、書いた文を捨てない。"""
    s, c = env
    i = draft(s, c)
    item = first(c, i)
    TestClient(create_app(s)).post(f"/drafts/{i}/items/{item['id']}/ok", data={"text": "本人は楽だと話した。"})
    assert first(c, i)["text"] == "本人は楽だと話した。" and not first(c, i)["flag"]


def test_ok_with_same_or_empty_text_only_clears_the_mark(env):
    s, c = env
    i = draft(s, c)
    item = first(c, i)
    TestClient(create_app(s)).post(f"/drafts/{i}/items/{item['id']}/ok", data={"text": ""})
    now = first(c, i)
    assert now["text"] == item["text"] and not now["flag"] and now["checked_flag"]


def test_count_is_in_one_place(env):
    s, c = env
    i = draft(s, c)
    view = draft_view(c, get_draft(c, i))
    n = len(view["checks"]) + len(view["blanks"]) + len(view["med_list"])
    html = page(s, i)
    assert f'<span class="draft-left">あと {n}件</span>' in html
    assert len(re.findall(r"あと \d+\s*件", html)) == 1 and "確かめること <span" not in html


def test_next_link_goes_to_the_first_thing_left(env):
    s, c = env
    i = draft(s, c)
    assert '<a class="draft-next" href="#f-place">' in page(s, i)
    set_blanks_aside(c, i)
    assert f'<a class="draft-next" href="#item-{first(c, i)["id"]}">' in page(s, i)


def test_nothing_left_moves_on_to_word(env):
    s, c = env
    i = draft(s, c)
    clear(c, i)
    set_blanks_aside(c, i)
    html = page(s, i)
    assert '<h1 class="do-line">Word で保存してください</h1>' in html
    assert re.search(r'<a class="main-link draft-word" href="/drafts/\d+/docx"', html)
    assert "draft-left" not in html and 'class="draft-next"' not in html and ">いいよ</button>" not in html


def test_word_is_not_blocked_and_says_what_is_left(env):
    s, c = env
    i = draft(s, c)
    view = draft_view(c, get_draft(c, i))
    n = len(view["checks"]) + len(view["blanks"])
    html = page(s, i)
    assert f'data-left="{n}"' in html and "残っていても保存できます" in html and 'class="plain-link draft-word"' in html
    assert TestClient(create_app(s)).get(f"/drafts/{i}/docx").status_code == 200


def test_medicine_stays_unconfirmed_and_visible_in_the_body(env):
    s, c = env
    i = draft(s, c, items=[MED])
    settle_draft(c, verdicts("発言どおり"), s, i)
    item = first(c, i)
    assert item["flag"]
    part = _item_part(page(s, i), item["id"])
    assert "to-check" in part and "薬・未確認" in part and item["quote"] in part and ">見た</button>" in part
    set_blanks_aside(c, i)
    html = page(s, i)
    assert '<h1 class="do-line">黄色の薬の文を見てください</h1>' in html and "あと 1件" in html  # 黙って 0 件にしない


def test_medicine_is_checked_only_when_the_person_presses(env):
    s, c = env
    i = draft(s, c, items=[MED])
    settle_draft(c, verdicts("発言どおり"), s, i)
    assert get_draft(c, i).flagged == 1
    TestClient(create_app(s)).post(f"/drafts/{i}/items/{first(c, i)['id']}/ok")
    assert get_draft(c, i).flagged == 0


def test_sample_mode_shows_red_without_inputs(env):
    s, c = env
    i = draft(s, c)
    html = TestClient(create_app(s, sample=True)).get(f"/drafts/{i}").text
    sheet = html.split("doc-sheet")[1].split("</article>")[0]
    assert "is-blank" in sheet and "<input" not in sheet and "<textarea" not in sheet
    assert ">いいよ</button>" not in html and "分からない" not in html


def test_screen_uses_the_in_place_script(env):
    s, c = env
    assert "/static/draft.js" in page(s, draft(s, c))
