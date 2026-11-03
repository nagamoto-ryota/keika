"""見本モードとデモで使うデータベースを、同梱の架空データから作り直す。実データは入らない。

  python tools/build_snapshot.py   書類の読み取りは今の見本の結果を流用する（API キーが要る）

基準日は 2026-11-13。見本の文字起こし（sample_data/demo_talks）を入れて、下書きと手順の案も見られるようにする。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO / "demo" / "snapshot" / "keika.db"
TALKS = ["会議_柚木_20261105_1400.txt", "訪問_柚木_20261112.txt"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    from keika.claude import METER, make_client
    from keika.claude_checks import run_claude_checks, save_run
    from keika.cli import AS_OF
    from keika.db import connect
    from keika.drafts import intake_with_drafts, list_drafts
    from keika.files import scan
    from keika.ledger import list_users, seed_from_json
    from keika.next_steps import make_plan
    from keika.reading import read_docs
    from keika.records import import_csv
    from keika.settings import Settings
    from keika.visit_questions import make_questions
    from keika.watcher import date_hint, scan_inbox
    from keika.web.app import current_findings

    with tempfile.TemporaryDirectory() as work:
        db = Path(work) / "keika.db"
        s = Settings(drive_root=REPO / "sample_data" / "drive", db_path=db, as_of=AS_OF)
        conn = connect(s)
        seed_from_json(conn, REPO / "sample_data" / "ledger.json")
        old = sqlite3.connect(SNAPSHOT)
        rows = old.execute("SELECT sha256, rel_path, facts, read_at FROM doc_facts").fetchall()
        old.close()
        conn.executemany("INSERT OR REPLACE INTO doc_facts (sha256, rel_path, facts, read_at) VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        client = make_client(REPO)
        for ref, facts in read_docs(conn, client, s, [r for r in scan(s) if r.scope != "inbox"]):
            if ref.ext == "csv" and facts.doc_type == "支援経過":
                import_csv(conn, client, s, ref)
        print("入口:", scan_inbox(conn, client, s))
        for name in TALKS:
            path = REPO / "sample_data" / "demo_talks" / name
            intake_with_drafts(conn, client, s, path.read_text(encoding="utf-8"), name, date_hint=date_hint(name))
        failed = []
        for user in list_users(conn):
            run = run_claude_checks(conn, client, s, user.id)
            save_run(conn, user.id, run)
            if run.not_run or run.error:
                failed.append(user.id)
        found, _ = current_findings(conn, s, "U04")
        target = next((f for f in found if "週3回" in f.message), None)
        plan = make_plan(conn, client, s, target) if target else None
        for user_id in ("U04", "U07"):  # 訪問前の1枚に、聞くことも入れておく
            make_questions(conn, client, s, user_id)
        bad = conn.execute("SELECT (SELECT COUNT(*) FROM records WHERE source = 'intake:mcp') + (SELECT COUNT(*) FROM review_items WHERE source = 'mcp')").fetchone()[0]
        print("下書き:", [(d.title, d.flagged) for d in list_drafts(conn)], "手順の案:", bool(plan), "点検の失敗:", failed, "mcp の行:", bad, f"約 ${METER.cost_usd:.2f}")
        if failed or bad or not plan:
            print("作り直しをやめました。今の見本はそのままです。")
            return 1
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        shutil.copyfile(db, SNAPSHOT)
    print("作り直しました:", SNAPSHOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
