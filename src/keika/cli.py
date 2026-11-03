"""起動の入口。keika serve（画面）、keika sync（Drive の読み取りと支援経過の取り込み）、keika check（Claude の点検）。"""
from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO / "demo" / "snapshot" / "keika.db"
AS_OF = date(2026, 11, 13)  # 見本の基準日。見本の文字起こしの日付に合わせる


def sample_settings(db_path: Path):
    """見本モードの設定。同梱の結果をコピーして使う（同梱の物は変えない）。"""
    from keika.settings import Settings

    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SNAPSHOT, db_path)
    return Settings(drive_root=REPO / "sample_data" / "drive", db_path=db_path, as_of=AS_OF)


def serve_sample(host: str, port: int) -> int:
    import uvicorn

    from keika.web.app import create_app

    def no_claude():
        raise RuntimeError("見本モードでは Claude を呼びません")

    s = sample_settings(REPO / "data" / "sample.db")
    print(f"見本モードで起動します（API キー不要）: http://{host}:{port}")
    uvicorn.run(create_app(s, client_factory=no_claude, sample=True), host=host, port=port, log_level="warning")
    return 0


def _seed_if_empty(conn, s) -> None:
    from keika.ledger import list_users, seed_from_json

    sample = s.drive_root.resolve().parent / "ledger.json"
    if not list_users(conn) and sample.is_file():
        seed_from_json(conn, sample)


def sync(user_prefix: str | None = None) -> int:
    from keika.claude import METER, make_client
    from keika.db import connect
    from keika.files import scan
    from keika.reading import read_docs
    from keika.records import import_csv
    from keika.settings import load_settings

    s = load_settings()
    conn = connect(s)
    _seed_if_empty(conn, s)
    client = make_client()
    refs = [r for r in scan(s) if r.scope != "inbox" and (not user_prefix or (r.user_folder or "").startswith(user_prefix))]
    started = time.time()
    results = read_docs(conn, client, s, refs)
    failed = [r.rel_path for r, f in results if not f.readable]
    for ref, facts in results:
        if ref.ext == "csv" and facts.doc_type == "支援経過":
            outcome = import_csv(conn, client, s, ref)
            print(f"{ref.rel_path}: 追加 {outcome.added}、変更 {len(outcome.changed)}、同じ {outcome.unchanged}{'、失敗: ' + outcome.error if outcome.error else ''}")
            if outcome.conflicts:
                print(f"  人が直した記録と CSV が違います（上書きしていません）: {len(outcome.conflicts)} 件")
            if outcome.bad_dates:
                print(f"  日付を読めなかった行: {outcome.bad_dates} 行")
    print(f"書類 {len(results)} 件、読めなかった {len(failed)} 件、{time.time() - started:.0f} 秒、API {METER.calls} 回、約 ${METER.cost_usd:.2f}")
    for name in failed:
        print(f"  読めなかった: {name}")
    return 0


def check(user_id: str) -> int:
    from keika.claude import make_client
    from keika.claude_checks import run_claude_checks, save_run
    from keika.db import connect
    from keika.settings import load_settings

    s = load_settings()
    conn = connect(s)
    started = time.time()
    run = run_claude_checks(conn, make_client(), s, user_id)
    save_run(conn, user_id, run)
    print(f"{user_id}: 指摘 {len(run.findings)} 件、取り下げ {len(run.dismissed)} 件、落とした {len(run.dropped)} 件、{time.time() - started:.0f} 秒、約 ${run.cost:.2f}")
    if run.not_run:
        print(f"未実施: {run.error}")
    for f in run.findings:
        print(f"- [{f.rule_id}/{f.kind}] {f.period} {f.message}")
        for e in f.evidence:
            print(f"    {e.file.rsplit('/', 1)[-1]} p{e.page} {e.record_date or ''} 「{e.quote}」")
        if f.searched_scope:
            print(f"    探した範囲: {f.searched_scope}")
        if f.suggestion:
            print(f"    案: {f.suggestion}")
    for d in run.dismissed:
        print(f"- 取り下げ [{d.rule_id}] {d.period} {d.reason}（{d.record_date}「{d.quote}」）")
    for d in run.dropped:
        print(f"- 落とした: {d.get('why')} {d.get('rule_id')} {str(d.get('message') or d.get('reason'))[:80]}")
    return 0


def intake_now() -> int:
    """入口のファイルを今すぐ取り込む。"""
    from keika.claude import make_client
    from keika.db import connect
    from keika.intake import open_reviews
    from keika.settings import load_settings
    from keika.watcher import scan_inbox

    s = load_settings()
    conn = connect(s)
    _seed_if_empty(conn, s)
    print(f"取り込み {scan_inbox(conn, make_client(), s)} 件、要確認 {len(open_reviews(conn))} 件")
    return 0


def serve(host: str, port: int, watch: bool = True) -> int:
    import uvicorn

    from keika.db import connect
    from keika.settings import load_settings
    from keika.web.app import create_app

    s = load_settings()
    conn = connect(s)
    _seed_if_empty(conn, s)
    conn.close()
    if watch:
        from keika.claude import make_client
        from keika.claude_checks import check_and_save
        from keika.watcher import start_watcher

        def on_saved(user_id: str) -> None:  # 届いた記録もその場で点検する
            threading.Thread(target=check_and_save, args=(s, make_client, user_id), daemon=True).start()

        start_watcher(s, make_client, on_saved=on_saved)
    if host == "0.0.0.0":
        print("注意: 同じネットワークの全員が、ログインなしで利用者の情報を見られます。事業所内のネットワークだけで使ってください。")
    print(f"ケイカを起動します: http://{host}:{port}")
    uvicorn.run(create_app(s), host=host, port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="keika")
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve", help="画面を起動する")
    p_serve.add_argument("--host", default="127.0.0.1", help="事業所内の他の PC から見る時は 0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-watch", action="store_true", help="入口のフォルダを見張らない")
    p_serve.add_argument("--sample", action="store_true", help="同梱の架空データの結果を、API キーなしで表示する")
    sub.add_parser("intake", help="入口のファイルを今すぐ取り込む")
    sub.add_parser("mcp", help="Claude Desktop から話した内容を受け取る MCP サーバーを動かす")
    p_sync = sub.add_parser("sync", help="Drive の書類を読み、支援経過の CSV を取り込む（Claude を使う）")
    p_sync.add_argument("--user", help="利用者フォルダの先頭（例: U04）")
    p_check = sub.add_parser("check", help="1人分を Claude で点検する")
    p_check.add_argument("user_id")
    args = parser.parse_args(argv)
    if args.command == "serve" and args.sample:
        return serve_sample(args.host, args.port)
    if args.command == "serve":
        return serve(args.host, args.port, watch=not args.no_watch)
    if args.command == "intake":
        return intake_now()
    if args.command == "mcp":
        from keika.mcp_server import run
        from keika.settings import load_settings

        return run(load_settings())
    if args.command == "check":
        return check(args.user_id)
    return sync(args.user)


if __name__ == "__main__":
    raise SystemExit(main())
