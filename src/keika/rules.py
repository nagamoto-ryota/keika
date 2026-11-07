"""点検表の読み込み。日付で自動に切り替わる。"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

RULES_PATH = Path(__file__).parent / "rules.yaml"
PENDING = "根拠確認中"


class Rule(BaseModel):
    id: str
    title: str
    plain: str
    layer: str
    legal_basis: str
    basis_confirmed: bool
    effective_from: date | None = None
    grace_until: date | None = None
    source: str
    source_url: str | None = None
    judge: str  # machine / claude / human
    done_when: str
    active: bool
    not_required: bool = False
    applies_to: str = "both"  # kyotaku / yobo / both
    added_by: str | None = None  # 通知から足した行は、その通知の名前


@lru_cache
def all_rules() -> tuple[Rule, ...]:
    return tuple(Rule(**row) for row in yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")))


def get_rule(rule_id: str) -> Rule:
    return next(r for r in all_rules() if r.id == rule_id)


def _in_force(rule: Rule, on: date) -> bool:
    if rule.effective_from and on < rule.effective_from:
        return False
    return not (rule.grace_until and on <= rule.grace_until)


def active_rules(on: date) -> list[Rule]:
    return [r for r in all_rules() if r.active and not r.not_required and _in_force(r, on)]


def basis_text(rule: Rule) -> str:
    """原文で確認できた根拠だけを条文として出す。"""
    return rule.legal_basis if rule.basis_confirmed else PENDING
