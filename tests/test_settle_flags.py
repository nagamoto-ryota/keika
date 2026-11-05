"""印の付いた文を Claude が話と照らし、発言どおりならカードにしない。薬の話は消さず、一覧に残す。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE
from keika.db import connect
from keika.draft_card import Verdict, Verdicts, draft_view, ok_item, revise_item, WireSentence, settle_draft, unsettled_drafts
from keika.drafts import WireDraft, WireItem, WireSection, get_draft, make_draft, to_docx
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.settings import Settings
from keika.web.app import create_app
from test_reading import FakeClient, answer

TALK = """2026年11月5日、柚木様のお宅で14時から訪問しました。薬の袋を見たら飲み忘れなしでした。
本人も昼のがないから楽だって言ってました。来る島先生にも伝えます。デイは週2回のまま続けます。"""
MED = WireItem(text="本人は昼の薬がなくなり楽だと話した。", quote="本人も昼のがないから楽だって言ってました")
NAME = WireItem(text="久留島医師に伝える。", quote="来る島先生にも伝えます")
ADDED = WireItem(text="デイは週3回のまま続ける。", quote="デイは週2回のまま続けます")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def draft(s, c, *items):
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="居宅訪問", monitoring=True, body="話した。"))
    wire = WireDraft(place="", start_time="14:00", end_time="", attendees=[], sections=[WireSection(key="services", items=list(items))])
    return make_draft(c, FakeClient(answer(wire)), s, r, TALK, "話.txt")


def verdicts(*said):
    return FakeClient(answer(Verdicts(items=[Verdict(number=n, verdict=v, why="") for n, v in enumerate(said)])))


def services(c, i):
    return get_draft(c, i).sections["services"]


def test_said_as_is_without_medicine_gets_no_card(env):
    s, c = env
    i = draft(s, c, NAME)
    settle_draft(c, verdicts("発言どおり"), s, i)
    item = services(c, i)[0]
    assert item["flag"] == "" and item["checked_flag"] and item["verdict"] == "発言どおり"
    assert draft_view(c, get_draft(c, i))["checks"] == []


def test_unsure_stays_a_card(env):
    s, c = env
    i = draft(s, c, ADDED)
    settle_draft(c, verdicts("人に聞く"), s, i)
    assert services(c, i)[0]["flag"] and len(draft_view(c, get_draft(c, i))["checks"]) == 1


def test_medicine_is_not_a_card_but_stays_marked_and_listed(env):
    s, c = env
    i = draft(s, c, MED)
    settle_draft(c, verdicts("発言どおり"), s, i)
    view = draft_view(c, get_draft(c, i))
    assert view["checks"] == [] and [m["text"] for m in view["med_list"]] == [MED.text]
    assert services(c, i)[0]["flag"]  # 未確認のまま
    html = TestClient(create_app(s)).get(f"/drafts/{i}").text
    assert "薬・未確認" in html and "to-check" in html and ">見た</button>" in html and ">いいよ</button>" not in html


def test_medicine_stays_marked_in_word(env):
    s, c = env
    i = draft(s, c, MED)
    settle_draft(c, verdicts("発言どおり"), s, i)
    assert get_draft(c, i).flagged == 1


def test_medicine_can_be_cleared_by_the_person(env):
    s, c = env
    i = draft(s, c, MED)
    settle_draft(c, verdicts("発言どおり"), s, i)
    ok_item(c, i, services(c, i)[0]["id"])
    assert draft_view(c, get_draft(c, i))["med_list"] == []


def test_medicine_unsure_stays_a_card(env):
    s, c = env
    i = draft(s, c, MED)
    settle_draft(c, verdicts("人に聞く"), s, i)
    assert len(draft_view(c, get_draft(c, i))["checks"]) == 1


def test_failure_leaves_every_card(env):
    s, c = env
    i = draft(s, c, NAME)

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("だめ")

    settle_draft(c, Broken(), s, i)
    assert len(draft_view(c, get_draft(c, i))["checks"]) == 1


def test_missing_answer_stays_a_card(env):
    s, c = env
    i = draft(s, c, NAME, ADDED)
    settle_draft(c, FakeClient(answer(Verdicts(items=[Verdict(number=1, verdict="人に聞く", why="")]))), s, i)
    assert len(draft_view(c, get_draft(c, i))["checks"]) == 2


def test_what_is_sent_includes_the_sentence_and_nearby_talk_only(env):
    s, c = env
    i = draft(s, c, NAME)
    client = verdicts("発言どおり")
    settle_draft(c, client, s, i)
    assert "久留島医師に伝える" in str(client.calls[0]["messages"]) and "来る島先生" in str(client.calls[0]["messages"])
    assert client.calls[0]["model"] == "claude-opus-5-5"


def test_each_draft_is_settled_once(env):
    s, c = env
    i = draft(s, c, ADDED)
    assert unsettled_drafts(c) == [i]
    settle_draft(c, verdicts("人に聞く"), s, i)
    assert unsettled_drafts(c) == []


def test_revised_sentence_is_a_person_decision_and_not_settled_again(env):
    s, c = env
    i = draft(s, c, ADDED)
    settle_draft(c, verdicts("人に聞く"), s, i)
    revise_item(c, FakeClient(answer(WireSentence(text="デイは週2回のまま続ける。"))), s, i, services(c, i)[0]["id"], "週2回です")
    assert unsettled_drafts(c) == []
    assert len(draft_view(c, get_draft(c, i))["checks"]) == 1


def test_opening_the_home_settles_in_the_background(env):
    s, c = env
    i = draft(s, c, NAME)
    http = TestClient(create_app(s, client_factory=lambda: verdicts("発言どおり")))
    http.get("/")
    assert services(c, i)[0]["flag"] == ""


def test_no_key_changes_nothing(env):
    s, c = env
    i = draft(s, c, NAME)
    assert TestClient(create_app(s)).get("/").status_code == 200
    assert services(c, i)[0]["flag"]


def test_sample_mode_does_not_settle(env):
    s, c = env
    i = draft(s, c, NAME)
    calls = []
    TestClient(create_app(s, client_factory=lambda: calls.append(1), sample=True)).get("/")
    assert calls == [] and services(c, i)[0]["flag"]


def test_word_output_still_works(env):
    s, c = env
    i = draft(s, c, MED)
    settle_draft(c, verdicts("発言どおり"), s, i)
    assert to_docx(get_draft(c, i), "柚木 茂", "")[:2] == b"PK"
