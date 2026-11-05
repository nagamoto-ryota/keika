"""試用の間に触った分を元に戻す道具。"""
import sqlite3

import pytest

from tools.reset_trial import reset


def db(path, value):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    c.execute("DELETE FROM t")
    c.execute("INSERT INTO t VALUES (?)", (value,))
    c.commit()
    c.close()


def read(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT v FROM t").fetchone()[0]
    finally:
        c.close()


def test_reset_restores_the_base(tmp_path):
    base, target = tmp_path / "trial_base.db", tmp_path / "desk_try.db"
    db(base, "元")
    db(target, "触った")
    reset(base, target)
    assert read(target) == "元" and read(base) == "元"


def test_reset_works_while_someone_holds_the_target_open(tmp_path):
    base, target = tmp_path / "trial_base.db", tmp_path / "desk_try.db"
    db(base, "元")
    db(target, "触った")
    held = sqlite3.connect(target)
    reset(base, target)
    assert held.execute("SELECT v FROM t").fetchone()[0] == "元"
    held.close()


@pytest.mark.parametrize("name", ["demo_clean.db", "keika.db", "desk_dev.db"])
def test_only_try_databases_can_be_overwritten(tmp_path, name):
    base, target = tmp_path / "trial_base.db", tmp_path / name
    db(base, "元")
    db(target, "大事")
    with pytest.raises(ValueError):
        reset(base, target)
    assert read(target) == "大事"
