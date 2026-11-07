from dataclasses import replace

from conftest import SAMPLE
from keika.ledger import User, find_person, get_user, list_users, seed_from_json, upsert_user, user_folder
from keika.settings import Settings


def seeded(conn):
    seed_from_json(conn, SAMPLE / "ledger.json")
    return conn


def test_seed_loads_8_users(conn):
    assert len(list_users(seeded(conn))) == 8


def test_seed_twice_does_not_duplicate(conn):
    seeded(conn)
    assert len(list_users(seeded(conn))) == 8


def test_list_users_filters_by_cm(conn):
    seeded(conn)
    assert {u.cm for u in list_users(conn, cm="CM02")} == {"CM02"}


def test_find_person_returns_all_same_names(conn):
    seeded(conn)
    found = {(m.kind, m.name) for m in find_person(conn, "洋子")}
    assert ("staff", "真柴 洋子") in found
    assert ("family", "戸叶 洋子") in found


def test_find_person_ignores_spaces_and_matches_kana(conn):
    seeded(conn)
    assert [m.user_id for m in find_person(conn, "杉森ハル") if m.kind == "user"] == ["U01"]
    assert [m.user_id for m in find_person(conn, "すぎもり はる") if m.kind == "user"] == ["U01"]


def test_family_match_knows_its_user(conn):
    seeded(conn)
    assert [m.user_id for m in find_person(conn, "戸叶 恵子")] == ["U05"]


def test_upsert_user_adds_and_edits(conn):
    u = User(id="U99", name="試験 太郎", kana="しけん たろう", cm="CM01")
    upsert_user(conn, u)
    upsert_user(conn, replace(u, name="試験 次郎"))
    assert get_user(conn, "U99").name == "試験 次郎"


def test_user_folder_matches_folder_with_id(conn, sample_settings):
    seeded(conn)
    assert user_folder(conn, sample_settings, "U01").name == "U01_杉森ハル"


def test_user_folder_matches_folder_without_id(conn, tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "杉森ハル").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db")
    upsert_user(conn, User(id="A1", name="杉森 ハル", kana="", cm=""))
    assert user_folder(conn, s, "A1").name == "杉森ハル"


def test_user_folder_is_none_when_missing(conn, sample_settings):
    upsert_user(conn, User(id="A1", name="居ない 人", kana="", cm=""))
    assert user_folder(conn, sample_settings, "A1") is None
