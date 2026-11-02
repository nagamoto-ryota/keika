from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record, get_record, history
from keika.settings import Settings
from keika.web.app import create_app


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-02", kind_written="訪問", kind_read="居宅訪問", body="もとの文。"))
    yield s, c, r, TestClient(create_app(s))
    c.close()


def save(client, r, **kw):
    data = dict(user_id=r.user_id, event_date=r.event_date, event_time="", kind_written="訪問", partner="", body=r.body, author="")
    return client.post(f"/records/{r.id}/edit", data=data | kw, follow_redirects=False)


def test_edit_form_saves_and_history_page_shows_both_versions(env):
    s, c, r, client = env
    assert "もとの文。" in client.get(f"/records/{r.id}/edit").text
    assert save(client, r, body="直した文。").status_code == 303
    assert get_record(c, r.id).body == "直した文。"
    page = client.get(f"/records/{r.id}/history").text
    assert "もとの文。" in page and "直した文。" in page


def test_history_page_has_no_delete_button(env):
    s, c, r, client = env
    assert "削除" not in client.get(f"/records/{r.id}/history").text


def test_unchecked_mark_can_be_cleared(env):
    s, c, r, client = env
    client.post(f"/records/{r.id}/checked")
    assert get_record(c, r.id).unchecked is False


def test_reassign_from_screen(env):
    s, c, r, client = env
    save(client, r, user_id="U01")
    assert get_record(c, r.id).user_id == "U01"
    assert [v.user_id for v in history(c, r.id)] == ["U04", "U01"]


def test_edit_cannot_change_written_at(env):
    s, c, r, client = env
    save(client, r, first_written_at="2000-01-01")
    assert get_record(c, r.id).first_written_at == r.first_written_at


def test_empty_body_is_refused(env):
    s, c, r, client = env
    assert save(client, r, body=" ").status_code == 400


def test_unknown_record_gives_404(env):
    s, c, r, client = env
    assert client.get("/records/99999/edit").status_code == 404
    assert client.get("/records/99999/history").status_code == 404
    assert client.post("/records/99999/checked").status_code == 404


def test_user_page_links_to_edit_and_history(env):
    s, c, r, client = env
    body = client.get("/users/U04").text
    assert f"/records/{r.id}/edit" in body and f"/records/{r.id}/history" in body and f"/records/{r.id}/checked" in body


def test_tests_never_see_a_real_key():
    from keika.claude import load_api_key
    assert load_api_key() is None
