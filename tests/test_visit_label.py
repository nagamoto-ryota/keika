"""画面での呼び名は「訪問時の聞き取りリスト」（11/7 長本さんの実機での声: 「訪問前の1枚」は意味が分かりにくい）。URL は /users/{id}/visit のまま。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.ledger import seed_from_json
from keika.settings import Settings
from keika.web.app import create_app

NEW, OLD = "訪問時の聞き取りリスト", "訪問前の1枚"


@pytest.fixture
def web(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield TestClient(create_app(s))
    c.close()


@pytest.mark.parametrize("path", ["/", "/users/U04", "/users/U04/visit", "/board"])
def test_screens_use_the_new_name(web, path):
    html = web.get(path).text
    assert NEW in html and OLD not in html, path


def test_the_address_stays_the_same(web):
    assert 'href="/users/U04/visit"' in web.get("/users/U04").text
    assert web.get("/users/U04/visit").status_code == 200
