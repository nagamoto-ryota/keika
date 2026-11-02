"""e-Gov 法令 API から基準省令を取り、条・項・号ごとの本文を src/keika/legal/ に保存する。

  python tools/fetch_law.py
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent.parent / "src" / "keika" / "legal"
LAWS = {"kyotaku": "411M50000100038", "yobo": "418M60000100037"}


def text(n) -> str:
    if isinstance(n, str):
        return n
    if isinstance(n, dict):
        return "" if n.get("tag") == "Rt" else "".join(text(c) for c in n.get("children", []))
    return "".join(text(c) for c in n)


def kids(n: dict, tag: str) -> list[dict]:
    return [c for c in n.get("children", []) if isinstance(c, dict) and c.get("tag") == tag]


def main_articles(node: dict):
    for c in node.get("children", []):
        if not isinstance(c, dict) or c.get("tag") == "SupplProvision":
            continue
        if c.get("tag") == "Article":
            yield c
        else:
            yield from main_articles(c)


def _name(num: str, unit: str) -> str:
    head, _, branch = num.partition("_")
    return f"{head}{unit}の{branch}" if branch else f"{head}{unit}"


def item_text(item: dict) -> str:
    lines = [text(kids(item, "ItemSentence"))]
    for sub in kids(item, "Subitem1"):
        lines.append(f"{text(kids(sub, 'Subitem1Title'))}　{text(kids(sub, 'Subitem1Sentence'))}")
    return "\n".join(lines)


def flatten(article: dict) -> dict[str, str]:
    art = _name(article["attr"]["Num"], "条")
    paragraphs = kids(article, "Paragraph")
    out: dict[str, str] = {}
    whole = []
    for p in paragraphs:
        items = kids(p, "Item")
        body = "\n".join([text(kids(p, "ParagraphSentence")), *[f"{text(kids(i, 'ItemTitle'))}　{item_text(i)}" for i in items]])
        para = "" if len(paragraphs) == 1 else f"{p['attr']['Num']}項"
        whole.append(body)
        if para:
            out[f"{art}{para}"] = body
        for i in items:
            out[f"{art}{para}{_name(i['attr']['Num'], '号')}"] = item_text(i)
    out[art] = "\n".join(whole)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for key, law_id in LAWS.items():
        with urllib.request.urlopen(f"https://laws.e-gov.go.jp/api/2/law_data/{law_id}?response_format=json", timeout=60) as r:
            data = json.load(r)
        info = data["revision_info"]
        sections: dict[str, str] = {}
        captions: dict[str, str] = {}
        for a in main_articles(data["law_full_text"]):
            sections.update(flatten(a))
            captions[_name(a["attr"]["Num"], "条")] = text(kids(a, "ArticleCaption")).strip("（）")
        (OUT / f"{key}.json").write_text(json.dumps({
            "title": info["law_title"], "law_num": data["law_info"]["law_num"], "enforced": info["amendment_enforcement_date"],
            "url": f"https://laws.e-gov.go.jp/law/{law_id}", "captions": captions, "sections": sections,
        }, ensure_ascii=False, indent=0), encoding="utf-8")
        print(key, info["law_title"], info["amendment_enforcement_date"], len(sections))


if __name__ == "__main__":
    main()
