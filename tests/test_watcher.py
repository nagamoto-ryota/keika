import os
import time
from datetime import date

import pytest

from conftest import SAMPLE
from keika.db import connect
from keika.intake import WireEntry, WireIntake, open_reviews
from keika.ledger import seed_from_json
from keika.records import list_records
from keika.settings import Settings
from keika.watcher import date_hint, handle_file, scan_inbox
from test_reading import FakeClient, answer


def wire():
    return WireIntake(entries=[WireEntry(
        user_id="U04", spoken_name="柚木さん", ambiguous=False, ambiguity_note="", event_date="2026-11-02", event_time="",
        kind_read="居宅訪問", monitoring=True, partner="本人", body="訪問した。", name_fixes=[], unsure_words=[])])


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "入口").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db", as_of=date(2026, 11, 4))
    c = connect(s)
    seed_from_json(c, SAMPLE / "ledger.json")
    yield s, c
    c.close()


def drop(s, name="口述.txt", text="訪問した。", age=60):
    path = s.drive_root / "入口" / name
    path.write_text(text, encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def test_new_file_is_processed_once(env):
    s, c = env
    path = drop(s)
    client = FakeClient(answer(wire()))
    assert handle_file(c, client, s, path) is True
    assert handle_file(c, client, s, path) is False
    assert len(client.calls) == 1 and len(list_records(c, "U04")) == 1


def test_same_content_is_not_processed_twice(env):
    s, c = env
    client = FakeClient(answer(wire()))
    handle_file(c, client, s, drop(s, "a.txt"))
    assert handle_file(c, client, s, drop(s, "別名.txt")) is False
    assert len(client.calls) == 1


def test_original_file_is_never_deleted(env):
    s, c = env
    path = drop(s)
    handle_file(c, FakeClient(answer(wire())), s, path)
    assert path.is_file() and path.read_text(encoding="utf-8") == "訪問した。"


def test_file_still_syncing_is_not_read_until_stable(env):
    s, c = env
    path = drop(s, age=0)
    client = FakeClient(answer(wire()))
    assert handle_file(c, client, s, path) is False and client.calls == []
    old = time.time() - 60
    os.utime(path, (old, old))
    assert handle_file(c, client, s, path) is True


def test_scan_picks_up_what_was_missed(env):
    s, c = env
    drop(s, "a.txt", "訪問した。")
    drop(s, "b.txt", "別の話。")
    client = FakeClient(answer(wire()))
    assert scan_inbox(c, client, s) == 2


def test_saved_user_is_reported(env):
    s, c = env
    called = []
    handle_file(c, FakeClient(answer(wire())), s, drop(s), on_saved=called.append)
    assert called == ["U04"]


def test_file_that_is_not_text_goes_to_review_box(env):
    s, c = env
    path = s.drive_root / "入口" / "b.pdf"
    path.write_bytes(b"broken")
    old = time.time() - 60
    os.utime(path, (old, old))
    client = FakeClient(answer(wire()))
    assert handle_file(c, client, s, path) is True
    assert client.calls == [] and "b.pdf" in open_reviews(c)[0]["note"]


def test_date_hint_reads_file_names():
    assert date_hint("口述01_井波_2026-11-02.txt") == "2026-11-02"
    assert date_hint("録音_20261102_1401.txt") == "2026-11-02"
    assert date_hint("Recording 2026-11-04 10-02.txt") == "2026-11-04"
    assert date_hint("メモ.txt") is None
