"""利用者の画面の「帳票」の欄。既存の画面へのリンクだけで、新しい AI 呼び出しは足さない。"""
import re
from datetime import date
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.drafts import WireDraft, WireItem, WireSection, make_draft
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer

ASSESS = "利用者/U04_柚木茂/アセスメント_2025-11-20.pdf"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def shelf(s, **kw):
    html = TestClient(create_app(s, **kw)).get("/users/U04").text
    return html.split('id="forms"')[1].split("</section>")[0]


def draft(s, c, kind):
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="会議" if kind == "meeting" else "居宅訪問", monitoring=kind == "monitoring", body="話した。"))
    key = "conclusion" if kind == "meeting" else "services"
    wire = WireDraft(place="", start_time="", end_time="", attendees=[], sections=[WireSection(key=key, items=[WireItem(text="デイは週2回のまま続ける。", quote="デイは週2回のまま続けます")])])
    return make_draft(c, FakeClient(answer(wire)), s, r, "デイは週2回のまま続けます", "話.txt")


def assessment(s, c):
    (s.drive_root / ASSESS).parent.mkdir(parents=True)
    (s.drive_root / ASSESS).write_bytes(b"%PDF-1.4 x")


def test_shelf_lists_the_forms_in_one_place(env):
    s, c = env
    part = shelf(s)
    for word in ("ケアプラン", "会議録", "モニタリング", "訪問前の1枚"):
        assert word in part
    assert 'href="/users/U04/visit"' in part


def test_no_plan_goes_to_the_existing_way_to_make_one(env):
    s, c = env
    part = shelf(s)
    assert 'action="/users/U04/careplan"' in part and "作る" in part


def test_existing_meeting_draft_opens(env):
    s, c = env
    i = draft(s, c, "meeting")
    part = shelf(s)
    assert f'href="/drafts/{i}"' in part and "会議の記録が入ると作れます" not in part


def test_no_meeting_record_says_how_it_comes(env):
    s, c = env
    part = shelf(s)
    assert "会議の記録が入ると作れます" in part
    assert "<button disabled" not in part


def test_existing_monitoring_draft_opens(env):
    s, c = env
    i = draft(s, c, "monitoring")
    assert f'href="/drafts/{i}"' in shelf(s)


def test_no_visit_record_says_how_monitoring_comes(env):
    s, c = env
    assert "訪問の記録が入ると作れます" in shelf(s)


def test_assessment_not_shown_when_there_is_none(env):
    s, c = env
    assert "アセスメント" not in shelf(s)


def test_assessment_opens_the_real_one_and_cannot_be_read_as_making(env):
    s, c = env
    assessment(s, c)
    part = shelf(s)
    row = next(line for line in part.splitlines() if "アセスメント" in line)
    assert f"/desk?t=U04&amp;doc={quote(ASSESS)}" in row
    assert "作" not in re.sub(r"<[^>]+>", "", row)


def test_desk_opens_the_named_document_only_if_it_is_this_persons(env):
    s, c = env
    assessment(s, c)
    client = TestClient(create_app(s))
    assert f'data-open-file="{ASSESS}"' in client.get("/desk", params={"t": "U04", "doc": ASSESS}).text
    assert "data-open-file" not in client.get("/desk", params={"t": "U05", "doc": ASSESS}).text


def test_sample_mode_has_no_make_button(env):
    s, c = env
    part = shelf(s, sample=True)
    assert "<form" not in part and "ケアプラン" in part


def test_preventive_plan_code_is_kept():
    from keika.care_plan import KINDS

    assert "yobo" in KINDS
