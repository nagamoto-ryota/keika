from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from keika.claude import MODEL, load_api_key, make_client
from keika.db import connect
from keika.files import scan
from keika.reading import WireFacts, read_doc, read_docs
from keika.settings import Settings


class FakeStream:
    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    """Claude の代わり。呼ばれた回数と渡された中身を覚え、用意した答えを順に返す。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        return FakeStream(self.results.pop(0) if len(self.results) > 1 else self.results[0])


def wire(**kw) -> WireFacts:
    blank = dict(
        readable=True, read_note="", doc_type="その他", body_status="不明", summary="", user_name="", doc_date="",
        doc_date_source=dict(page=0, quote=""), period_start="", period_end="", period_source=dict(page=0, quote=""),
        target_month="", signature="不明", signature_date="", signature_source=dict(page=0, quote=""),
        consent_date="", consent_source=dict(page=0, quote=""), consent_persons=[], issues=[], goals=[], services=[],
        delivered_to=[], attendees=[], activities=[],
    )
    return WireFacts(**(blank | kw))


def answer(facts, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(parsed_output=facts, stop_reason=stop_reason, usage=usage)


FACTS = wire(summary="読めた")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    (root / "事業所").mkdir(parents=True)
    s = Settings(drive_root=root, db_path=tmp_path / "x.db")
    (root / "事業所" / "a.txt").write_text("本文", encoding="utf-8")
    c = connect(s)
    yield s, c
    c.close()


def only_ref(s):
    return scan(s)[0]


def test_same_sha_is_not_read_twice(env):
    s, conn = env
    client = FakeClient(answer(FACTS))
    read_doc(conn, client, s, only_ref(s))
    again = read_doc(conn, client, s, only_ref(s))
    assert len(client.calls) == 1
    assert again.summary == "読めた"


def test_changed_file_is_read_again(env):
    s, conn = env
    client = FakeClient(answer(FACTS))
    read_doc(conn, client, s, only_ref(s))
    (s.drive_root / "事業所" / "a.txt").write_text("変わった", encoding="utf-8")
    read_doc(conn, client, s, only_ref(s))
    assert len(client.calls) == 2


def test_unreadable_doc_gets_unknown_facts(env):
    s, conn = env
    (s.drive_root / "事業所" / "a.txt").unlink()
    (s.drive_root / "事業所" / "b.pdf").write_bytes(b"broken")
    client = FakeClient(answer(FACTS))
    facts = read_doc(conn, client, s, only_ref(s))
    assert client.calls == []
    assert facts.readable is False and facts.signature_present is None


def test_invalid_json_is_retried_once_then_unknown(env):
    s, conn = env
    client = FakeClient(ValueError("JSON が壊れている"))
    facts = read_doc(conn, client, s, only_ref(s))
    assert len(client.calls) == 2
    assert facts.readable is False and facts.signature_present is None


def test_invalid_json_then_success_is_used(env):
    s, conn = env
    client = FakeClient(ValueError("壊れている"), answer(FACTS))
    assert read_doc(conn, client, s, only_ref(s)).summary == "読めた"


def test_failed_read_is_not_saved_and_is_tried_again(env):
    s, conn = env
    read_doc(conn, FakeClient(ValueError("壊れている")), s, only_ref(s))
    client = FakeClient(answer(FACTS))
    assert read_doc(conn, client, s, only_ref(s)).summary == "読めた"


def test_api_failure_gives_unknown_without_raising(env):
    s, conn = env
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    facts = read_doc(conn, FakeClient(err), s, only_ref(s))
    assert facts.readable is False


def test_refusal_gives_unknown(env):
    s, conn = env
    facts = read_doc(conn, FakeClient(answer(None, stop_reason="refusal")), s, only_ref(s))
    assert facts.readable is False


def test_text_doc_is_sent_as_text_with_file_name(env):
    s, conn = env
    client = FakeClient(answer(FACTS))
    read_doc(conn, client, s, only_ref(s))
    kw = client.calls[0]
    assert kw["model"] == MODEL == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    joined = str(kw["messages"])
    assert "a.txt" in joined and "本文" in joined


def test_pdf_is_sent_as_document(sample_settings, conn):
    ref = next(r for r in scan(sample_settings) if r.ext == "pdf")
    client = FakeClient(answer(FACTS))
    read_doc(conn, client, sample_settings, ref)
    blocks = client.calls[0]["messages"][0]["content"]
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"


def test_read_docs_keeps_order_and_skips_cached(env):
    s, conn = env
    (s.drive_root / "事業所" / "b.txt").write_text("別", encoding="utf-8")
    refs = scan(s)
    client = FakeClient(answer(FACTS))
    read_doc(conn, client, s, refs[0])
    out = read_docs(conn, client, s, refs)
    assert [r.rel_path for r, _ in out] == [r.rel_path for r in refs]
    assert len(client.calls) == 2


def test_prompt_has_no_sample_answers():
    # 手順に架空データの穴の中身を書かない
    from keika import reading

    for word in ("杉森", "柚木", "綿貫", "U04", "H02"):
        assert word not in reading.SYSTEM


def test_load_api_key_prefers_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    (tmp_path / ".env").write_text('# メモ\nANTHROPIC_API_KEY="from-file"\n', encoding="utf-8")
    assert load_api_key(tmp_path) == "from-file"


def test_load_api_key_uses_env_without_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert load_api_key(tmp_path) == "from-env"


def test_make_client_without_key_gives_clear_error(tmp_path, monkeypatch):
    # ant の別の認証や他の組織へ黙って流れないよう、キーが無ければ止める
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API キー"):
        make_client(Path(tmp_path))


def test_wire_schema_fits_structured_output_limits():
    # 公式の上限: 省略可は合計24、union は合計16。全項目を必須にして union を使わない
    from anthropic.lib._parse._transform import transform_schema

    schema = transform_schema(WireFacts)

    def walk(node):
        if isinstance(node, dict):
            assert "anyOf" not in node and not isinstance(node.get("type"), list)
            if isinstance(node.get("properties"), dict) and node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)


def test_wire_converts_blanks_to_none():
    facts = wire().to_facts()
    assert facts.doc_date is None and facts.doc_date_source is None and facts.signature_present is None


def test_wire_converts_signature_and_sources():
    facts = wire(
        signature="なし",
        signature_source=dict(page=2, quote="署名欄"),
        services=[dict(name="通所介護", per_week=-1, days="", source=dict(page=0, quote="週2回"))],
    ).to_facts()
    assert facts.signature_present is False
    assert facts.signature_source.page == 2
    assert facts.services[0].per_week is None and facts.services[0].days is None
    assert facts.services[0].source.page is None and facts.services[0].source.quote == "週2回"
    assert wire(signature="あり").to_facts().signature_present is True


def test_unreadable_answer_from_claude_is_kept_as_not_readable(env):
    s, conn = env
    facts = read_doc(conn, FakeClient(answer(wire(readable=False, read_note="白紙"))), s, only_ref(s))
    assert facts.readable is False and facts.read_note == "白紙"
