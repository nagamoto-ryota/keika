"""読み取りと整文のモデルを比べる。架空データを読み直し、費用、時間、読み取った項目を残す。

  python eval/compare_models.py --model claude-sonnet-5-5 --label sonnet

同じモデルを2回回すと、同じモデルどうしの揺れ（比べる時の土台）が分かる。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "eval"))

from run_boundary import CASES  # noqa: E402

from keika.claude import METER, make_client  # noqa: E402
from keika.db import connect  # noqa: E402
from keika.files import scan  # noqa: E402
from keika.intake import open_reviews, process_text  # noqa: E402
from keika.ledger import seed_from_json  # noqa: E402
from keika.med_names import message, open_checks  # noqa: E402
from keika.reading import read_docs  # noqa: E402
from keika.settings import Settings  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True)
    args = ap.parse_args()
    os.environ["KEIKA_MODEL_READ"] = os.environ["KEIKA_MODEL_INTAKE"] = args.model
    out_dir = REPO / "data" / "compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(drive_root=REPO / "sample_data" / "drive", db_path=out_dir / f"{args.label}.db", as_of=date(2026, 11, 4))
    conn = connect(s)
    seed_from_json(conn, REPO / "sample_data" / "ledger.json")
    client = make_client(REPO)

    started, before = time.time(), METER.cost_usd
    results = read_docs(conn, client, s, [r for r in scan(s) if r.scope != "inbox"])
    reading = {"docs": len(results), "unreadable": sum(not f.readable for _, f in results), "seconds": round(time.time() - started), "cost_usd": round(METER.cost_usd - before, 3)}
    print("読み取り:", reading)

    started, before = time.time(), METER.cost_usd
    intake = []
    inbox = sorted((s.drive_root / "入口").glob("*.txt"))
    sources = [{"id": p.name, "text": p.read_text(encoding="utf-8")} for p in inbox] + [{"id": c["id"], "text": c["text"], "must_keep": c["must_keep"], "must_not": c["must_not"]} for c in CASES]
    for src in sources:
        one = time.time()
        rows = []
        for r in process_text(conn, client, s, src["text"], "compare"):
            if r.record:
                rows.append({"user": r.record.user_id, "kind": r.record.kind_read, "date": r.record.event_date, "saved": True, "body": r.record.body})
            else:
                item = next(i for i in open_reviews(conn) if i["id"] == r.review_id)
                rows.append({"user": item["entry"].get("user_id"), "saved": False, "body": item["entry"].get("body", ""), "warnings": item["warnings"], "note": item["note"]})
        text = " ".join(x["body"] for x in rows)
        intake.append({**src, "rows": rows, "seconds": round(time.time() - one, 1), "missing": [w for w in src.get("must_keep", []) if w not in text], "crossed": [w for w in src.get("must_not", []) if w in text]})
    summary = {"seconds": round(time.time() - started), "cost_usd": round(METER.cost_usd - before, 3), "reviews": sum(not x["saved"] for i in intake for x in i["rows"]),
               "crossed": sum(len(i["crossed"]) for i in intake), "missing": sum(len(i["missing"]) for i in intake),
               "med_checks": [" ".join(message(m)) for m in open_checks(conn, s)]}
    print("整文:", summary)
    (out_dir / f"{args.label}.json").write_text(json.dumps({"model": args.model, "reading": reading, "intake_summary": summary, "intake": intake}, ensure_ascii=False, indent=1), encoding="utf-8")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
