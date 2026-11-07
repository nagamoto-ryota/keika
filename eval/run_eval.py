"""測定。架空データを読み取り、点検し、判定表と照らして結果を eval/results/ に残す。

使い方（リポのフォルダで）:
    python eval/run_eval.py --users U03,U05
    python eval/run_eval.py --facts-from data/keika.db   読み取りの保存結果を流用する（費用を抑える）
    python eval/run_eval.py --fresh                      読み取りからやり直す
    python eval/run_eval.py --with-inbox                 入口の素材を取り込んだ後で測る
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "eval"))

from scoring import Score, load_judge_table, score  # noqa: E402

from keika.claude import METER, make_client  # noqa: E402
from keika.claude_checks import run_claude_checks, save_run  # noqa: E402
from keika.db import connect  # noqa: E402
from keika.files import scan  # noqa: E402
from keika.findings import Finding  # noqa: E402
from keika.ledger import list_users, seed_from_json  # noqa: E402
from keika.office import office_findings  # noqa: E402
from keika.reading import read_docs  # noqa: E402
from keika.records import import_csv  # noqa: E402
from keika.settings import Settings  # noqa: E402
from keika.watcher import scan_inbox  # noqa: E402
from keika.web.app import current_findings  # noqa: E402

AS_OF = date(2026, 11, 4)


def prepare(db_path: Path, facts_from: Path | None) -> Settings:
    s = Settings(drive_root=REPO / "sample_data" / "drive", db_path=db_path, as_of=AS_OF)
    conn = connect(s)
    seed_from_json(conn, REPO / "sample_data" / "ledger.json")
    if facts_from:
        source = sqlite3.connect(facts_from)
        rows = source.execute("SELECT sha256, rel_path, facts, read_at FROM doc_facts").fetchall()
        source.close()
        conn.executemany("INSERT OR REPLACE INTO doc_facts (sha256, rel_path, facts, read_at) VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    conn.close()
    return s


def load_drive(s: Settings, client, users: list[str] | None, with_inbox: bool) -> None:
    conn = connect(s)
    refs = [r for r in scan(s) if r.scope != "inbox" and (r.scope == "office" or not users or (r.user_folder or "")[:3] in users)]
    for ref, facts in read_docs(conn, client, s, refs):
        if ref.ext == "csv" and facts.doc_type == "支援経過":
            outcome = import_csv(conn, client, s, ref)
            if outcome.error:
                print(f"取り込み失敗: {ref.rel_path}: {outcome.error}")
    if with_inbox:
        print(f"入口の取り込み: {scan_inbox(conn, client, s)} 件")
    conn.close()


def check_user(s: Settings, client, user_id: str) -> tuple[str, list[Finding], dict]:
    conn = connect(s)
    started = time.time()
    run = run_claude_checks(conn, client, s, user_id)
    save_run(conn, user_id, run)
    found, _ = current_findings(conn, s, user_id)
    conn.close()
    return user_id, found, {"seconds": round(time.time() - started), "cost": round(run.cost, 3), "not_run": run.not_run,
                            "error": run.error, "dropped": len(run.dropped), "dismissed": [asdict(d) for d in run.dismissed]}


def summarize(sc: Score) -> str:
    finding_total = sum(v["total"] for v in sc.by_kind.values())
    lines = [
        f"指摘の穴: {len(sc.hits)} / {finding_total} （{sc.by_kind}）",
        f"先回りの穴: {len(sc.nudge_hits)} / {len(sc.nudge_hits) + len(sc.nudge_misses)}",
        f"罠で誤って指摘: {sc.false_positives}　黙れた: {sc.silent_ok}　要判定: {sc.silent_review}",
        f"「分からない」が正解の項目: 正しく出た {sc.unknown_item_correct} / 断定した {sc.unknown_item_errors}",
        f"見つけられなかった穴: {sc.misses}",
        f"場所は合うが語が無い（言い換えかも。別の係が判定）: {len(sc.reason_review)} 件",
        f"表に無い指摘（別の係が判定）: {len(sc.unlisted)} 件",
        f"「分からない」の指摘: {len(sc.unknowns)} 件",
        f"利用者あたりの指摘: {sc.per_user}",
    ]
    return "\n".join(lines)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", help="U03,U05 のように絞る")
    parser.add_argument("--db", help="測定用のデータベース。省略すると結果フォルダに新しく作る")
    parser.add_argument("--facts-from", help="読み取りの保存結果を流用するデータベース")
    parser.add_argument("--fresh", action="store_true", help="読み取りからやり直す（--facts-from を無視する）")
    parser.add_argument("--with-inbox", action="store_true")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    results = REPO / "eval" / "results"
    results.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}{'-' + args.label if args.label else ''}"
    db_path = Path(args.db) if args.db else REPO / "data" / "eval" / f"{name}.db"
    users = args.users.split(",") if args.users else None
    s = prepare(db_path, None if args.fresh else (Path(args.facts_from) if args.facts_from else None))
    client = make_client(REPO)
    started = time.time()
    load_drive(s, client, users, args.with_inbox)
    conn = connect(s)
    targets = [u.id for u in list_users(conn) if not users or u.id in users]
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(lambda uid: check_user(s, client, uid), targets))
    findings = [f for _, found, _ in runs for f in found]
    if not users:
        findings += office_findings(conn, s)
    table = load_judge_table(REPO / "eval" / "judge_table.yaml")
    if users:
        table = [i for i in table if set(i["利用者ID"] if isinstance(i["利用者ID"], list) else [i["利用者ID"]]) & set(users)]
    sc = score(findings, table, with_inbox=args.with_inbox)
    print(summarize(sc))
    print(f"\n{time.time() - started:.0f} 秒、API {METER.calls} 回、約 ${METER.cost_usd:.2f}")
    out = {
        "at": stamp, "label": args.label, "users": users, "with_inbox": args.with_inbox, "fresh": args.fresh, "seconds": round(time.time() - started),
        "cost_usd": round(METER.cost_usd, 2), "runs": {uid: info for uid, _, info in runs},
        "score": {k: v for k, v in asdict(sc).items() if k not in ("unknowns", "unlisted")},
        "unknowns": [f.model_dump() for f in sc.unknowns],
        "unlisted": [f.model_dump() for f in sc.unlisted],
        "findings": [f.model_dump() for f in findings],
    }
    path = results / f"{name}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"結果: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
