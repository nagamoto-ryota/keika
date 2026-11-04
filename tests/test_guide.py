"""導入の手引き: 画面から開けて印刷でき、同意書を Word で保存できる。"""
import io
import re
from datetime import date

import docx
import pytest
from fastapi.testclient import TestClient

from keika.settings import Settings
from keika.web.app import create_app


@pytest.fixture
def client(tmp_path):
    (tmp_path / "drive").mkdir()
    return TestClient(create_app(Settings(drive_root=tmp_path / "drive", db_path=tmp_path / "x.db", as_of=date(2026, 11, 13))))


def test_footer_links_the_guide(client):
    assert 'href="/guide"' in client.get("/guide").text.split("<footer")[1]
    assert 'href="/guide"' in client.get("/").text.split("<footer")[1]


def test_guide_covers_what_the_office_does_before_entering_data(client):
    page = client.get("/guide").text
    for word in ("同意", "家族", "追記", "学習", "保存期間", "処理する国", "取扱規程", "委託先", "職員のルール", "事故", "廃棄", "退職", "BitLocker", "画面ロック", "バックアップ", "平文", "印刷する"):
        assert word in page, word


def test_guide_says_it_is_not_legal_advice(client):
    page = client.get("/guide").text
    assert "法的助言ではありません" in page and "専門家" in page


def test_guide_leaves_provider_facts_for_the_office_to_write(client):
    """国名や期間は規約で変わるので、手引きが断定しない。"""
    page = client.get("/guide").text
    assert 'class="fill"' in page
    for word in ("アメリカ", "米国", "30日", "30 日", "Anthropic"):
        assert word not in page, word


def test_guide_has_no_unchecked_article_numbers(client):
    assert not re.search(r"第\s*\d+\s*条", client.get("/guide").text)


def test_guide_opens_in_sample_mode(tmp_path):
    (tmp_path / "drive").mkdir()
    s = Settings(drive_root=tmp_path / "drive", db_path=tmp_path / "x.db")
    assert TestClient(create_app(s, client_factory=lambda: None, sample=True)).get("/guide").status_code == 200


def test_consent_word_keeps_the_template(client):
    r = client.get("/guide/consent.docx")
    assert r.status_code == 200 and "wordprocessingml" in r.headers["content-type"]
    d = docx.Document(io.BytesIO(r.content))
    text = "\n".join([p.text for p in d.paragraphs] + [c.text for t in d.tables for row in t.rows for c in row.cells])
    for word in ("記録の作成に AI を使うことについてのご説明と同意書", "［事業所名］", "［AI の提供元の名称］", "［国名］", "□ 同意します", "ご家族（ご自身の情報についても同意される方）", "関係者のお名前"):
        assert word in text, word
    for mark in ("##", "|", "---", "`", "たたき台", "法的な助言"):  # 利用者に渡す書類に、事業所向けの前書きを残さない
        assert mark not in text, mark
    assert d.paragraphs[0].style.name == "Heading 1" and "同意書" in d.paragraphs[0].text
    assert round(d.sections[0].page_width.mm) == 210 and round(d.sections[0].page_height.mm) == 297
    assert any(row.cells[0].text == "提供先" for t in d.tables for row in t.rows)


def test_bundled_consent_matches_the_document_in_docs():
    from pathlib import Path
    from keika.guide import CONSENT
    assert CONSENT.parent.name == "keika"
    assert CONSENT.read_bytes() == (Path(__file__).parent.parent / "docs" / "consent-template.md").read_bytes()


def test_consent_word_saves_in_sample_mode(tmp_path):
    (tmp_path / "drive").mkdir()
    s = Settings(drive_root=tmp_path / "drive", db_path=tmp_path / "x.db")
    assert TestClient(create_app(s, client_factory=lambda: None, sample=True)).get("/guide/consent.docx").status_code == 200


def test_guide_tells_what_to_do_for_people_who_do_not_agree(client):
    page = client.get("/guide").text
    assert "台帳にも読み取りフォルダにも入れず" in page and "音声" in page and "API キー" in page


def test_guide_sentence_stays_whole_when_printed(client):
    assert 'class="no-print" href="/guide/consent.docx"' not in client.get("/guide").text


def test_guide_makes_no_promises_it_cannot_keep(client):
    page = client.get("/guide").text
    assert "外部へ送らない" not in page and "スマホ" not in page
