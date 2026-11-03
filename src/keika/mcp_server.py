"""Claude Desktop から話した内容を受け取る MCP サーバー。

別の抜け道にしないため、入口のファイルと同じ process_text を通す。
架空データの実演専用。そのデータベース専用の鍵のファイル（keika.db なら keika.db.mcp_on）が無ければ、Claude にもデータベースにも触らずに断る。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from keika.claude import make_client
from keika.db import connect
from keika.drafts import intake_with_drafts
from keika.ledger import get_user
from keika.settings import Settings

REPO = Path(__file__).resolve().parents[2]
LOCKED = "この道具は止めています。何も記録していません。架空データの実演の時だけ、準備をした上で使えます。"
TOOL_DESCRIPTION = (
    "架空データの実演専用の道具です。実在の利用者の話には使いません。"
    "準備をしていない時は、何も記録せずに断ります。"
    "実演では、話した言葉をそのまま text に渡します。要約や言い換えをせず、人名や薬名を直さない。"
    "返ってきた結果を、そのまま伝えます。"
)


def key_path(s: Settings) -> Path:
    return s.db_path.with_name(s.db_path.name + ".mcp_on")


def enabled(s: Settings) -> bool:
    return key_path(s).is_file()


def record_text(s: Settings, client: Any, text: str) -> str:
    if not enabled(s):
        return LOCKED
    if not text.strip():
        return "話した内容が空だったので、何も記録していません。"
    conn = connect(s)
    try:
        lines = []
        for result in intake_with_drafts(conn, client, s, text, "mcp"):
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
        if not enabled(s):
            return LOCKED
        return record_text(s, client_factory(), text)

    return server


def run(s: Settings) -> int:
    """Claude Desktop はどのフォルダから起動するか決まらないので、相対の場所はアプリのフォルダから解決する。"""
    fixed = Settings(drive_root=REPO / s.drive_root, db_path=REPO / s.db_path, as_of=s.as_of)
    build_server(fixed, lambda: make_client(REPO)).run("stdio")
    return 0
