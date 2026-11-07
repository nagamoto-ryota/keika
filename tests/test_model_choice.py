"""用途ごとのモデルの切り替えと、モデルごとの費用の計算。"""
from types import SimpleNamespace

from keika.claude import MODEL, PRICES, UsageMeter, model_for


def usage(i=1_000_000, o=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def test_default_is_opus(monkeypatch):
    monkeypatch.delenv("KEIKA_MODEL_READ", raising=False)
    assert model_for("READ") == MODEL == "claude-opus-5-5"


def test_override_per_task(monkeypatch):
    monkeypatch.setenv("KEIKA_MODEL_READ", "claude-haiku-5-5")
    assert model_for("READ") == "claude-haiku-5-5" and model_for("INTAKE") == MODEL


def test_cost_uses_the_price_of_the_model_called():
    m = UsageMeter()
    m.add(usage(), "claude-opus-5-5")
    m.add(usage(), "claude-haiku-5-5")
    assert abs(m.cost_usd - (PRICES["claude-opus-5-5"][0] + PRICES["claude-haiku-5-5"][0])) < 1e-9
