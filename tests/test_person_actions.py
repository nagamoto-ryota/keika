"""その人の事は、その人の画面で全部できる。画面の上の方に、行き先を1列のボタンでまとめる（11/6「窓口がとっ散らかってて分かりづらい」）。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.ledger import seed_from_json
from keika.settings import Settings
from keika.web.app import create_app


@pytest.fixture
def env(tmp_path):
    s = Settings(drive_root=SAMPLE / "drive", db_path=tmp_path / "keika.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def row(s, uid="U04"):
    html = TestClient(create_app(s)).get(f"/users/{uid}").text
    return html, html.split('class="person-actions')[1].split("</nav>")[0]


def test_one_row_of_buttons_comes_right_under_the_name(env):
    html, actions = row(env[0])
    for label in (">記録<", ">AI相談<", ">ケアプラン<", ">アセスメント<", ">モニタリング<", ">メール<", ">訪問前の1枚<"):
        assert label in actions, label
    assert html.index('class="person-actions') < html.index('id="forms"')


def test_each_button_goes_to_this_persons_place(env):
    html, actions = row(env[0])
    assert 'href="#kiroku"' in actions and 'id="kiroku"' in html
    assert 'href="/desk?t=U04"' in actions and 'href="/users/U04/visit"' in actions
    for anchor in ("#form-plan", "#form-assessment", "#form-monitoring"):
        assert f'href="{anchor}"' in actions and f'id="{anchor[1:]}"' in html


def test_mail_button_opens_the_open_mail_of_this_person(env):
    s, c = env
    c.execute("INSERT INTO desk_mails (id, thread, to_name, subject, body, created_at) VALUES (77, 'U04', 'ヘルパーほたるい', '件名', '本文', '2026-11-04')")
    c.execute("INSERT INTO mail_out (mail_id, user_id, record_id, recipient_id, purpose, ground, origin, state, created_at) VALUES (77, 'U04', 0, 'x', '連絡', '根拠', 'proposal', 'open', '2026-11-04')")
    c.commit()
    _, actions = row(s)
    assert 'href="/desk/U04/mail/77"' in actions
