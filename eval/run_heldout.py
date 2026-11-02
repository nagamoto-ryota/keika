"""確認用データの測定。答えを見ない: 件数と費用だけを画面に出し、指摘の中身は採点の係だけが読む。

  python eval/run_heldout.py --source <確認用データのフォルダ> --label 1

渡すのは drive/利用者 と ledger_add.json だけ。コピーの上で帳票を標準様式に置き直してから測る（元は触らない）。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "eval"))

import standard_forms  # noqa: E402
import standard_plans  # noqa: E402

from keika.claude import METER, make_client  # noqa: E402
from keika.claude_checks import run_claude_checks, save_run  # noqa: E402
from keika.db import connect  # noqa: E402
from keika.files import scan  # noqa: E402
from keika.ledger import seed_from_json  # noqa: E402
from keika.reading import read_docs  # noqa: E402
from keika.records import import_csv  # noqa: E402
from keika.settings import Settings  # noqa: E402
from keika.web.app import current_findings  # noqa: E402

USERS = ["V01", "V02", "V03"]


def restyle_copy(root: Path) -> dict[str, int]:
    """中身を画面に出さないよう、道具の出力は捨てて件数だけ返す。"""
    counts: Counter = Counter()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for p in sorted(root.rglob(standard_forms.GLOB)):
            counts["要点:" + ("済み" if standard_forms.restyle(p) else "変更なし")] += 1
        for g in standard_plans.GLOBS:
            for p in sorted(root.rglob(g)):
                if p.suffix in (".pdf", ".docx"):
                    counts["計画書:" + standard_plans.restyle(p)] += 1
    return dict(counts)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--label", default="1")
    args = ap.parse_args()
    source = Path(args.source)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = REPO / "data" / "heldout" / stamp
    shutil.copytree(source / "drive" / "利用者", work / "drive" / "利用者")
    print("置き直し:", restyle_copy(work / "drive"))
    s = Settings(drive_root=work / "drive", db_path=work / "x.db", as_of=date(2026, 11, 4))
    conn = connect(s)
    seed_from_json(conn, source / "ledger_add.json")
    seed_from_json(conn, REPO / "sample_data" / "ledger.json")  # 職員と事業所
    client = make_client(REPO)
    started = time.time()
    unread = imported_errors = 0
    for ref, facts in read_docs(conn, client, s, scan(s)):
        unread += facts is None or facts.doc_type == "読めなかった"
        if ref.ext == "csv" and facts.doc_type == "支援経過":
            imported_errors += bool(import_csv(conn, client, s, ref).error)
    runs, findings = {}, []
    for uid in USERS:
        run = run_claude_checks(conn, client, s, uid)
        save_run(conn, uid, run)
        found, _ = current_findings(conn, s, uid)
        findings += [f.model_dump() for f in found]
        runs[uid] = {"not_run": run.not_run, "dropped": len(run.dropped), "dismissed": [asdict(d) for d in run.dismissed], "error": run.error}
    out = {"at": stamp, "label": args.label, "seconds": round(time.time() - started), "cost_usd": round(METER.cost_usd, 2), "runs": runs, "findings": findings, "work": str(work)}
    path = REPO / "eval" / "results" / f"{stamp}-heldout-{args.label}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"読めなかった {unread}、取り込み失敗 {imported_errors}、指摘 {len(findings)} 件、{out['seconds']} 秒、約 ${out['cost_usd']}")
    print("未実施:", {u: bool(r["not_run"] or r["error"]) for u, r in runs.items()})
    print("結果:", path)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
