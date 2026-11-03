"""Claude Desktop から話した内容を受け取る MCP サーバー。

別の抜け道にしないため、入口のファイルと同じ process_text を通す。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from keika.claude import make_client
from keika.db import connect
from keika.intake import process_text
from keika.ledger import get_user
from keika.settings import Settings

REPO = Path(__file__).resolve().parents[2]
TOOL_DESCRIPTION = (
    "ケアマネジャーが話した内容を、ケイカの支援経過の下書きとして記録します。"
    "利用者が話した言葉をそのまま text に渡します。要約や言い換えをせず、人名や薬名を直さない。"
    "返ってきた結果を、そのまま利用者に伝えます。"
)


def record_text(s: Settings, client: Any, text: str) -> str:
    if not text.strip():
        return "話した内容が空だったので、何も記録していません。"
    conn = connect(s)
    try:
        lines = []
        for result in process_text(conn, client, s, text, "mcp"):
            if result.record:
                user = get_user(conn, result.record.user_id)
                lines.append(f"{user.name}さんの {result.record.event_date} の記録を、未確認の下書きとして保存しました。内容を確かめて決めるのはご本人です。")
            else:
                why = f"（確かめたい語: {'、'.join(result.warnings)}）" if result.warnings else ""
                lines.append(f"記録にはせず、やることの「記録の確認」に回しました{why}。ケイカの画面で確かめてください。")
        return "\n".join(lines)
    finally:
        conn.close()


def build_server(s: Settings, client_factory: Callable[[], Any]):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("keika")

    @server.tool(name="record_text", description=TOOL_DESCRIPTION)
    def record_text_tool(text: str) -> str:
        return record_text(s, client_factory(), text)

    return server


def run(s: Settings) -> int:
    """Claude Desktop はどのフォルダから起動するか決まらないので、相対の場所はアプリのフォルダから解決する。"""
    fixed = Settings(drive_root=REPO / s.drive_root, db_path=REPO / s.db_path, as_of=s.as_of)
    build_server(fixed, lambda: make_client(REPO)).run("stdio")
    return 0
