import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from keika.settings import OutsideRootError, Settings, load_settings, now_real, safe_path, today


def make(tmp_path: Path, as_of: date | None = None) -> Settings:
    root = tmp_path / "drive"
    root.mkdir()
    return Settings(drive_root=root, db_path=tmp_path / "keika.db", as_of=as_of)


def test_today_uses_as_of_when_set(tmp_path):
    assert today(make(tmp_path, date(2026, 11, 4))) == date(2026, 11, 4)


def test_today_uses_clock_without_as_of(tmp_path):
    assert today(make(tmp_path)) == date.today()


def test_now_real_ignores_as_of(tmp_path):
    make(tmp_path, date(2000, 1, 1))
    assert abs(now_real() - datetime.now().astimezone()) < timedelta(minutes=1)


def test_load_settings_reads_as_of_and_root(tmp_path, monkeypatch):
    monkeypatch.setenv("KEIKA_DRIVE_ROOT", str(tmp_path))
    monkeypatch.setenv("KEIKA_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("KEIKA_AS_OF", "2026-11-04")
    s = load_settings()
    assert s.drive_root == tmp_path
    assert s.as_of == date(2026, 11, 4)


def test_safe_path_accepts_inside(tmp_path):
    s = make(tmp_path)
    (s.drive_root / "利用者").mkdir()
    assert safe_path(s, "利用者") == (s.drive_root / "利用者").resolve()


def test_safe_path_rejects_parent_escape(tmp_path):
    s = make(tmp_path)
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "answer_key.csv").write_text("x")
    with pytest.raises(OutsideRootError):
        safe_path(s, "../eval/answer_key.csv")


def test_safe_path_rejects_absolute_outside(tmp_path):
    s = make(tmp_path)
    with pytest.raises(OutsideRootError):
        safe_path(s, str(tmp_path / "keika.db"))


def test_safe_path_rejects_symlink_outside(tmp_path):
    s = make(tmp_path)
    secret = tmp_path / "answer_key.csv"
    secret.write_text("x")
    link = s.drive_root / "link.csv"
    try:
        os.symlink(secret, link)
    except OSError:
        pytest.skip("この環境ではシンボリックリンクを作れない")
    with pytest.raises(OutsideRootError):
        safe_path(s, "link.csv")
