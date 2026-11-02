"""音声入力: OS 標準を主な入口にする。ブラウザのボタンは端末内の認識専用で、外部へ送る認識に切り替えない。"""
from pathlib import Path

from test_intake import app_client, entry, env, run  # noqa: F401

from keika.intake import _SPOKEN_GAP, WireIntake, join_spoken

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


def test_spaces_between_spoken_words_are_removed():
    assert join_spoken("今日 お 宅 に 行き まし た") == "今日お宅に行きました"
    assert join_spoken("薬 は 1 錠 です") == "薬は1錠です"
    assert join_spoken("血圧 は 1 2 0") == "血圧は1 2 0"
    assert join_spoken("一行目 です" + chr(10) + "二行目 です") == "一行目です" + chr(10) + "二行目です"


def test_spoken_dose_with_spaces_is_not_mistaken_for_an_invented_one(env):  # noqa: F811
    s, c = env
    results, _ = run(s, c, "柚木 さん 宅 を 訪問 薬 は 1 錠 変わり なし", entry(body="柚木様宅を訪問した。薬は1錠で変わりなし。"))
    assert results[0].record is not None


def test_not_taken_with_spaces_still_catches_the_reversed_meaning(env):  # noqa: F811
    s, c = env
    results, _ = run(s, c, "柚木 さん 宅 を 訪問 薬 は 飲め て は い ない", entry(body="柚木様宅を訪問した。薬は飲めている。"))
    assert results[0].review_id is not None and "飲めている" in results[0].warnings


def test_button_joins_spoken_words_on_screen():
    js = JS.read_text(encoding="utf-8")
    assert "joinSpoken(finals + interim)" in js and f"/{_SPOKEN_GAP.pattern}/g" in js
