"""裏の照合や AI の直しが Claude を待つ間に、人が下書きを直しても、人の直しを消さない（11/7 最終レビュー 🔴）。"""
from datetime import date

import pytest

from conftest import SAMPLE
from keika.db import connect
from keika.draft_card import SAID, Verdict, Verdicts, WireSentence, edit_item, ok_item, revise_item, settle_draft
from keika.drafts import WireDraft, WireItem, WireSection, get_draft, make_draft
from keika.ledger import seed_from_json
from keika.records import RecordIn, add_record
from keika.settings import Settings
from test_reading import FakeClient, answer

TALK = "デイは週2回のまま続けます。本人も昼のがないから楽だって言ってました。"
THREE = WireItem(text="デイは週3回のまま続ける。", quote="デイは週2回のまま続けます")
EASY = WireItem(text="本人は楽だと話した。", quote="本人はとても楽だと言った")  # 話に無い引用なので印が付く


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 5))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def draft(s, c, items):
    r = add_record(c, RecordIn(user_id="U04", event_date="2026-11-05", kind_read="居宅訪問", monitoring=True, body="話した。"))
    w = WireDraft(place="", start_time="", end_time="", attendees=[], sections=[WireSection(key="services", items=items)])
    return make_draft(c, FakeClient(answer(w)), s, r, TALK, "話.txt")


class Meanwhile(FakeClient):
    """Claude が答える前に、人の操作を1回はさむ。"""

    def __init__(self, during, *results):
        super().__init__(*results)
        self.during = during

    def _stream(self, **kw):
        self.during()
        return super()._stream(**kw)


def items(c, i):
    return get_draft(c, i).sections["services"]


def test_settling_keeps_a_sentence_the_person_fixed_meanwhile(env):
    s, c = env
    i = draft(s, c, [THREE])
    item_id = items(c, i)[0]["id"]
    fixed = "デイは週2回のまま続ける。"
    client = Meanwhile(lambda: edit_item(c, i, item_id, fixed), answer(Verdicts(items=[Verdict(number=0, verdict=SAID, why="同じ")])))
    settle_draft(c, client, s, i)
    after = next(x for x in items(c, i) if x["text"] == fixed)
    assert not after["flag"] and "verdict" not in after


def test_settling_keeps_the_ok_the_person_gave_meanwhile_and_still_settles_the_rest(env):
    s, c = env
    i = draft(s, c, [THREE, EASY])
    first, second = items(c, i)
    client = Meanwhile(lambda: ok_item(c, i, first["id"]), answer(Verdicts(items=[Verdict(number=0, verdict="人に聞く", why="違う"), Verdict(number=1, verdict=SAID, why="同じ")])))
    settle_draft(c, client, s, i)
    a, b = items(c, i)
    assert not a["flag"] and a.get("checked_flag")  # 人の「いいよ」は消えない
    assert b["verdict"] == SAID


def test_revising_does_not_overwrite_a_sentence_the_person_fixed_meanwhile(env):
    s, c = env
    i = draft(s, c, [THREE])
    item_id = items(c, i)[0]["id"]
    fixed = "デイは週2回のまま続ける（人が直した）。"
    client = Meanwhile(lambda: edit_item(c, i, item_id, fixed), answer(WireSentence(text="デイは週2回のまま続ける。")))
    notice = revise_item(c, client, s, i, item_id, "週2回です")
    assert any(x["text"] == fixed for x in items(c, i)) and notice


@pytest.mark.parametrize("act", ["edit", "ok", "form"])
def test_a_person_s_save_waits_while_the_ai_writes_back(env, act):
    """AI が読み直して書き戻す間は、人の保存も待つ。どちらかが黙って消えない（11/7 追加レビュー）。"""
    import threading
    from keika import drafts
    from keika.drafts import update_draft
    s, c = env
    i = draft(s, c, [THREE])
    item_id = items(c, i)[0]["id"]
    d = get_draft(c, i)
    run = {
        "edit": lambda: edit_item(c, i, item_id, "人の文。"),
        "ok": lambda: ok_item(c, i, item_id),
        "form": lambda: update_draft(c, i, place=d.place, start_time=d.start_time, end_time=d.end_time, attendees=d.attendees, sections={"services": [{"id": item_id, "text": "人の文。"}]}),
    }[act]
    done = threading.Event()
    with drafts.WRITE:
        t = threading.Thread(target=lambda: (run(), done.set()))
        t.start()
        assert not done.wait(0.3)  # AI が書いている間は保存しない
    t.join(5)
    assert done.is_set()
