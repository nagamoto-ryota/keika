"""AI相談の入れ物の例文は、その人の記録に合う物にする。転倒の記録が無い人に「夜の転倒が続いてる」と見せない。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app

FALL = "例: 夜の転倒が続いてる。計画で足りない所を一緒に考えたい"


@pytest.fixture
def env(tmp_path):
    s = Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "keika.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def page(s, uid="U04"):
    return TestClient(create_app(s)).get(f"/desk?t={uid}").text


def test_no_fall_example_without_a_fall_record(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-01", kind_read="電話", body="長女から、デイが楽しいと連絡があった。"))
    html = page(s)
    assert FALL not in html and "例: 最近の記録から、次の訪問で確かめることを一緒に考えたい" in html


def test_fall_example_when_the_records_have_a_fall(env):
    s, c = env
    add_record(c, RecordIn(user_id="U04", event_date="2026-11-01", kind_read="電話", body="長女から、夜中にトイレへ行く時に転んだと連絡があった。"))
    assert FALL in page(s)
