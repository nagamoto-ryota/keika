"""音声入力: OS 標準を主な入口にする。ブラウザのボタンは端末内の認識専用で、外部へ送る認識に切り替えない。"""
from pathlib import Path

from test_intake import app_client, env  # noqa: F401

from keika.intake import WireIntake

JS = Path(__file__).parent.parent / "src" / "keika" / "web" / "static" / "speech.js"


def test_button_never_falls_back_to_sending_audio():
    js = JS.read_text(encoding="utf-8")
    assert "processLocally = true" in js
    assert "processLocally = false" not in js


def test_button_depends_on_local_availability():
    js = JS.read_text(encoding="utf-8")
    assert "Recognition.available" in js and "processLocally: true" in js and "Recognition.install" in js


def test_button_is_hidden_until_local_recognition_is_confirmed(env):  # noqa: F811
    s, c = env
    body = app_client(s, WireIntake(entries=[])).get("/records/new").text
    assert 'id="mic" class="plain" hidden' in body


def test_page_guides_to_os_voice_typing(env):  # noqa: F811
    s, c = env
    body = app_client(s, WireIntake(entries=[])).get("/records/new").text
    assert "Win + H" in body and "音声入力" in body
    assert "スマホ" not in body
    assert "外部へ送りません" not in body and "送られません" not in body
