"""訪問前の1枚に、その画面で直接入力する。"""
import pytest
from fastapi.testclient import TestClient

from keika.intake import IntakeResult
from keika.visit_notes import get_notes, save_note
from keika.web.app import create_app
from test_reading import FakeClient, answer
from test_visit_questions import env, wire  # noqa: F401


def note(client, user="U09", section="基本情報", label="趣味: 聞く", answer="将棋", **kw):
    return client.post(f"/users/{user}/visit/notes", data={"section": section, "label": label, "answer": answer, **kw})


def app_client(s, ai=None):
    return TestClient(create_app(s, client_factory=lambda: FakeClient(ai if ai is not None else RuntimeError("呼ばない"))))


def test_saved_answer_returns_to_its_question(env):
    s, c = env
    client = app_client(s, answer(wire()))
    client.post("/users/U09/visit/questions")
    page = client.get("/users/U09/visit").text
    assert 'class="note"' in page and "ここに入力" in page
    label = next(x["label"] for x in _labels(page) if x["section"] == "アセスメントで聞けていないこと")
    assert note(client, section="アセスメントで聞けていないこと", label=label, answer="段差あり<b>").status_code == 200
    page = client.get("/users/U09/visit").text
    assert ">段差あり&lt;b&gt;</textarea>" in page and "項目が変わった入力" not in page


def _labels(page):
    import html
    import re
    return [{"section": html.unescape(a), "label": html.unescape(b)} for a, b in re.findall(r'data-section="([^"]*)" data-label="([^"]*)"', page)]


def test_answer_is_not_lost_when_the_question_goes_away(env):
    s, c = env
    client = app_client(s)
    assert note(client, section="今確かめること", label="もう無い項目", answer="電話で確認済み").status_code == 200
    page = client.get("/users/U09/visit").text
    assert "項目が変わった入力" in page and "もう無い項目" in page and "電話で確認済み" in page


def test_checklist_answer_stays_selected(env):
    s, c = env
    client = app_client(s, answer(wire()))
    client.post("/users/U09/visit/questions")
    page = client.get("/users/U09/visit").text
    label = next(x["label"] for x in _labels(page) if x["section"] == "基本チェックリスト" and "階段" in x["label"])
    note(client, section="基本チェックリスト", label=label, answer="いいえ")
    page = client.get("/users/U09/visit").text
    assert 'value="いいえ" checked' in page and "項目が変わった入力" not in page


def test_unknown_user_cannot_save(env):
    s, c = env
    assert note(app_client(s), user="U999").status_code == 404


def test_sample_mode_keeps_typing_but_says_it_is_not_saved(env):
    s, c = env
    client = TestClient(create_app(s, sample=True))
    assert note(client).status_code == 403
    page = client.get("/users/U09/visit").text
    assert "見本のため保存されません" in page and "記録の入力欄へ送る" not in page


def test_send_to_record_prefills_the_form_with_answers_only(env):
    s, c = env
    client = app_client(s)
    note(client)
    save_note(c, s, "U09", "基本情報", "家族構成: 聞く", "")
    page = client.post("/users/U09/visit/to-record", follow_redirects=False)
    assert page.status_code == 200
    assert "趣味: 聞く" in page.text and "将棋" in page.text and "家族構成" not in page.text
    assert 'name="visit_user" value="U09"' in page.text
    assert "様の訪問" in page.text


def test_send_with_no_answers_goes_back(env):
    s, c = env
    r = app_client(s).post("/users/U09/visit/to-record", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/users/U09/visit"


def test_recording_clears_only_what_was_sent(env, monkeypatch):
    s, c = env
    client = app_client(s)
    note(client)
    form = client.post("/users/U09/visit/to-record").text
    import re
    stamp = re.search(r'name="visit_stamp" value="([^"]+)"', form).group(1)
    monkeypatch.setattr("keika.web.app.process_text", lambda *a, **k: [IntakeResult(review_id=1)])
    monkeypatch.setattr("keika.web.app.open_reviews", lambda conn: [], raising=False)
    note(client, label="後で書いた項目", answer="送った後の入力")  # 送った後に足した分は残す
    client.post("/records/new", data={"text": "柚木様の訪問", "visit_user": "U09", "visit_stamp": stamp})
    left = get_notes(c, "U09")
    assert [n["label"] for n in left.values()] == ["後で書いた項目"]


def test_failed_recording_keeps_answers(env, monkeypatch):
    s, c = env
    client = app_client(s)
    note(client)
    form = client.post("/users/U09/visit/to-record").text
    import re
    stamp = re.search(r'name="visit_stamp" value="([^"]+)"', form).group(1)

    def boom(*a, **k):
        raise RuntimeError("切断")
    monkeypatch.setattr("keika.web.app.process_text", boom)
    with pytest.raises(RuntimeError):
        client.post("/records/new", data={"text": "x", "visit_user": "U09", "visit_stamp": stamp})
    assert len(get_notes(c, "U09")) == 1


def test_answer_after_recording_can_be_written_again(env):
    s, c = env
    save_note(c, s, "U09", "基本情報", "趣味: 聞く", "将棋")
    from keika.visit_notes import mark_used
    mark_used(c, "U09", "9999")
    assert get_notes(c, "U09") == {}
    save_note(c, s, "U09", "基本情報", "趣味: 聞く", "囲碁")
    assert [n["answer"] for n in get_notes(c, "U09").values()] == ["囲碁"]
