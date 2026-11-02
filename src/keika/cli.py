"""起動の入口。keika serve（画面）、keika sync（Drive の読み取りと支援経過の取り込み）。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


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
    print(f"書類 {len(results)} 件、読めなかった {len(failed)} 件、{time.time() - started:.0f} 秒、API {METER.calls} 回、約 ${METER.cost_usd:.2f}")
    for name in failed:
        print(f"  読めなかった: {name}")
    return 0


def serve(host: str, port: int) -> int:
    import uvicorn

    from keika.db import connect
    from keika.settings import load_settings
    from keika.web.app import create_app

    s = load_settings()
    conn = connect(s)
    _seed_if_empty(conn, s)
    conn.close()
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
    p_sync = sub.add_parser("sync", help="Drive の書類を読み、支援経過の CSV を取り込む（Claude を使う）")
    p_sync.add_argument("--user", help="利用者フォルダの先頭（例: U04）")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.host, args.port)
    return sync(args.user)


if __name__ == "__main__":
    raise SystemExit(main())
