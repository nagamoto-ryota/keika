"""MCP の受け口: 話した内容を入口と同じ整文と検査に通す。別の抜け道にしない。"""
import asyncio
import os
import sys

import pytest

from conftest import SAMPLE
from test_intake import entry, env  # noqa: F401
from test_reading import FakeClient, answer

from keika.intake import WireIntake, open_reviews
from keika.mcp_server import TOOL_DESCRIPTION, record_text
from keika.records import list_records


def client(*entries):
    return FakeClient(answer(WireIntake(entries=list(entries))))


def test_record_text_uses_same_intake(env):  # noqa: F811
    s, c = env
    told = record_text(s, client(entry()), "柚木さん宅を訪問。変わりなし。")
    r = list_records(c, "U04")[0]
    assert r.source == "intake:mcp" and r.unchecked is True
    assert "柚木" in told and "未確認" in told


def test_talk_that_fails_the_checks_goes_to_a_person(env):  # noqa: F811
    s, c = env
    told = record_text(s, client(entry(body="柚木様宅を訪問した。受診が必要。")), "柚木さん宅を訪問。変わりなし。")
    assert list_records(c, "U04") == [] and len(open_reviews(c)) == 1
    assert "記録の確認" in told and "保存しました" not in told


def test_each_person_in_the_talk_is_reported(env):  # noqa: F811
    s, c = env
    other = entry(user_id="U01", spoken_name="杉森さん", kind_read="電話", body="杉森様に電話した。")
    told = record_text(s, client(entry(), other), "柚木さん宅を訪問。そのあと杉森さんに電話。")
    assert "柚木" in told and "杉森" in told


def test_empty_talk_is_not_sent_to_claude(env):  # noqa: F811
    s, c = env
    fake = client(entry())
    told = record_text(s, fake, "  \n ")
    assert "空" in told and list_records(c, "U04") == []
    assert not getattr(fake, "calls", [])


def test_tool_tells_claude_to_pass_the_words_as_spoken():
    assert "そのまま" in TOOL_DESCRIPTION and "直さない" in TOOL_DESCRIPTION


def test_tool_is_found_when_voice_input_hears_keika_as_keika_the_word():
    # 音声入力は「ケイカ」を「経過」と書く
    assert "ケイカ" in TOOL_DESCRIPTION and "「経過に記録」" in TOOL_DESCRIPTION


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
