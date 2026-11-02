"""根拠の原文。点検表の根拠の書きぶりから、保存した省令の本文と解釈通知・Q&A を引く。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from keika.rules import get_rule

LEGAL_DIR = Path(__file__).parent / "legal"
SOURCES_DIR = LEGAL_DIR / "sources"
LAW_PREFIX = {"居宅基準": "kyotaku", "予防基準": "yobo"}
SHORT_NAME = {"kyotaku": "居宅の基準", "yobo": "予防の基準"}


@dataclass(frozen=True)
class Section:
    law: str
    ref: str
    caption: str
    text: str
    url: str

    @property
    def label(self) -> str:
        ref = re.sub(r"(?<=[条項])(?=\d)", "第", self.ref)
        return f"{SHORT_NAME[self.law]} 第{ref}" + (f"（{self.caption}）" if self.caption else "")


@dataclass(frozen=True)
class Notice:
    rule: str
    doc: str
    where: str
    source: str
    url: str
    quote: str


@dataclass
class Basis:
    kyotaku: list[Section] = field(default_factory=list)
    yobo: list[Section] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)


@lru_cache
def _law(key: str) -> dict:
    return json.loads((LEGAL_DIR / f"{key}.json").read_text(encoding="utf-8"))


def section(law: str, ref: str) -> Section:
    data = _law(law)
    article = re.match(r"\d+条(?:の\d+)?", ref).group()
    return Section(law, ref, data["captions"].get(article, ""), data["sections"][ref], data["url"])


def parse_refs(basis: str) -> list[tuple[str, str]]:
    """「居宅基準13条9号・15号、予防基準30条9号」を (法, 条項号) の並びにする。"""
    refs: list[tuple[str, str]] = []
    law = None
    for segment in re.sub(r"（.*?）", "", basis).split("、"):
        for prefix, key in LAW_PREFIX.items():
            if segment.startswith(prefix):
                law, segment = key, segment[len(prefix):]
        if law is None or not re.match(r"\d", segment):
            continue
        article = None
        for token in segment.split("・"):
            found = re.match(r"(\d+条(?:の\d+)?)(.*)", token)
            if found:
                article, rest = found.groups()
            elif re.match(r"\d", token):
                rest = token
            else:
                continue  # イ・ロ・ハ は号の本文に含まれる
            rest = re.sub(r"[イロハニホ]$", "", rest)
            refs.append((law, article if "〜" in rest else f"{article}{rest}"))
    return refs


@lru_cache
def load_notices() -> tuple[Notice, ...]:
    path = LEGAL_DIR / "notices.yaml"
    if not path.exists():
        return ()
    return tuple(Notice(**row) for row in yaml.safe_load(path.read_text(encoding="utf-8")) or [])


def basis_for(rule_id: str) -> Basis:
    """確認できた根拠だけを返す。根拠確認中の行は空。"""
    rule = get_rule(rule_id)
    basis = Basis(notices=[n for n in load_notices() if n.rule == rule_id])
    if not rule.basis_confirmed:
        return basis
    for law, ref in parse_refs(rule.legal_basis):
        getattr(basis, law).append(section(law, ref))
    return basis
