from pathlib import Path

from fastapi.testclient import TestClient

from keika.cli import SNAPSHOT, sample_settings as make_sample_settings
from keika.settings import Settings
from keika.web.app import create_app


def forbidden():
    raise AssertionError("見本モードで Claude を呼んだ")


def sample_app(tmp_path: Path):
    s = make_sample_settings(tmp_path / "sample.db")
    return s, TestClient(create_app(s, client_factory=forbidden, sample=True))


def test_snapshot_is_in_the_repo():
    assert SNAPSHOT.is_file()


def test_sample_mode_runs_without_api_key(tmp_path):
    s, client = sample_app(tmp_path)
    body = client.get("/").text
    assert "やること" in body and "見本モード" in body and "デモ基準日" in body


def test_sample_mode_shows_records_and_findings(tmp_path):
    s, client = sample_app(tmp_path)
    assert "支援経過" in client.get("/users/U05").text
    assert client.get("/office").status_code == 200


def test_sample_mode_never_calls_claude(tmp_path):
    s, client = sample_app(tmp_path)
    r = client.post("/records/new", data={"text": "訪問した。", "mode": "shape"})
    assert r.status_code == 403 and "見本モード" in r.text


def test_sample_mode_copy_does_not_touch_the_snapshot(tmp_path):
    before = SNAPSHOT.read_bytes()
    s, client = sample_app(tmp_path)
    client.post("/ledger", data={"id": "", "name": "試験 花子", "kana": "", "cm": "", "care_level": "", "cert_start": "", "cert_end": "", "type": ""})
    assert SNAPSHOT.read_bytes() == before


def test_empty_ledger_and_plain_name_folders_can_start(tmp_path):
    root = tmp_path / "drive"
    (root / "利用者" / "田中花子").mkdir(parents=True)
    (root / "入口").mkdir()
    s = Settings(drive_root=root, db_path=tmp_path / "x.db")
    client = TestClient(create_app(s))
    for path in ("/", "/board", "/office", "/ledger", "/review", "/records/new", "/silenced"):
        assert client.get(path).status_code == 200, path
    client.post("/ledger", data={"id": "", "name": "田中 花子", "kana": "", "cm": "", "care_level": "要介護1", "cert_start": "", "cert_end": "", "type": "居宅介護支援"})
    assert "田中 花子" in client.get("/").text
