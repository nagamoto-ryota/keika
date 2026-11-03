"""MCP の受け口: 話した内容を入口と同じ整文と検査に通す。別の抜け道にしない。"""
import asyncio
import os
import sys

import pytest

from conftest import SAMPLE
from test_intake import entry, env  # noqa: F401
from test_reading import FakeClient, answer

from keika.intake import WireIntake, open_reviews
from keika.mcp_server import KEY_NAME, TOOL_DESCRIPTION, build_server, record_text
from keika.records import list_records
from keika.settings import Settings


def client(*entries):
    return FakeClient(answer(WireIntake(entries=list(entries))))


@pytest.fixture
def unlocked(env):  # noqa: F811
    s, c = env
    (s.db_path.parent / KEY_NAME).touch()
    return s, c


def test_record_text_uses_same_intake(unlocked):
    s, c = unlocked
    told = record_text(s, client(entry()), "柚木さん宅を訪問。変わりなし。")
    r = list_records(c, "U04")[0]
    assert r.source == "intake:mcp" and r.unchecked is True
    assert "柚木" in told and "未確認" in told


def test_talk_that_fails_the_checks_goes_to_a_person(unlocked):
    s, c = unlocked
    told = record_text(s, client(entry(body="柚木様宅を訪問した。受診が必要。")), "柚木さん宅を訪問。変わりなし。")
    assert list_records(c, "U04") == [] and len(open_reviews(c)) == 1
    assert "記録の確認" in told and "保存しました" not in told


def test_each_person_in_the_talk_is_reported(unlocked):
    s, c = unlocked
    other = entry(user_id="U01", spoken_name="杉森さん", kind_read="電話", body="杉森様に電話した。")
    told = record_text(s, client(entry(), other), "柚木さん宅を訪問。そのあと杉森さんに電話。")
    assert "柚木" in told and "杉森" in told


def test_empty_talk_is_not_sent_to_claude(unlocked):
    s, c = unlocked
    fake = client(entry())
    told = record_text(s, fake, "  \n ")
    assert "空" in told and list_records(c, "U04") == []
    assert not getattr(fake, "calls", [])


def test_tool_tells_claude_to_pass_the_words_as_spoken():
    assert "そのまま" in TOOL_DESCRIPTION and "直さない" in TOOL_DESCRIPTION


def test_tool_says_it_is_for_fictional_demos_only():
    assert "架空" in TOOL_DESCRIPTION and "実演" in TOOL_DESCRIPTION
    assert "他を探さず" not in TOOL_DESCRIPTION and "「経過に記録」" not in TOOL_DESCRIPTION


def test_without_key_nothing_is_sent_and_nothing_is_written(tmp_path):
    s = Settings(drive_root=tmp_path, db_path=tmp_path / "x.db")
    fake = client(entry())
    told = record_text(s, fake, "実在しない話。訪問した。")
    assert "止めて" in told
    assert fake.calls == []
    assert not s.db_path.exists()  # DB にも触らない


def test_without_key_rows_do_not_increase(env):  # noqa: F811
    s, c = env
    before = c.execute("SELECT (SELECT COUNT(*) FROM records) + (SELECT COUNT(*) FROM review_items)").fetchone()[0]
    record_text(s, client(entry()), "柚木さん宅を訪問。変わりなし。")
    assert c.execute("SELECT (SELECT COUNT(*) FROM records) + (SELECT COUNT(*) FROM review_items)").fetchone()[0] == before


def test_tool_does_not_even_make_a_client_without_key(tmp_path):
    pytest.importorskip("mcp")
    s = Settings(drive_root=tmp_path, db_path=tmp_path / "x.db")

    def forbidden():
        raise AssertionError("鍵が無いのに Claude の接続を作った")

    server = build_server(s, forbidden)
    result = asyncio.run(server.call_tool("record_text", {"text": "話"}))
    assert "止めて" in str(result)


def test_key_in_another_folder_does_not_unlock(tmp_path):
    (tmp_path / KEY_NAME).touch()
    s = Settings(drive_root=tmp_path, db_path=tmp_path / "sub" / "x.db")
    assert "止めて" in record_text(s, client(entry()), "話")


def test_real_protocol_lists_the_tool(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def tools():
        environment = {**os.environ, "KEIKA_DRIVE_ROOT": str(SAMPLE / "drive"), "KEIKA_DB_PATH": str(tmp_path / "x.db"), "PYTHONIOENCODING": "utf-8"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "keika.cli", "mcp"], env=environment)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return (await session.list_tools()).tools

    found = asyncio.run(asyncio.wait_for(tools(), 60))
    assert [t.name for t in found] == ["record_text"]
    assert found[0].description == TOOL_DESCRIPTION
