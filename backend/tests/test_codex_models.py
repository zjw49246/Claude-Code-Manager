"""GPT-5.6 是三个模型（sol/terra/luna），不存在裸 "gpt-5.6" ID。

证据：Codex CLI 0.144.6 服务端模型列表（~/.codex/models_cache.json）：
sol/terra 支持 effort low..ultra，luna 支持 low..max，gpt-5.5 及更早只到 xhigh。
"""

from backend.config import settings
from backend.services.codex_models import (
    CODEX_MODEL_EFFORTS,
    clamp_codex_effort,
    supported_codex_efforts,
)

GPT56_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


def _option_list() -> list[str]:
    return [m.strip() for m in settings.codex_model_options.split(",") if m.strip()]


def test_codex_model_options_contain_all_three_gpt56_models():
    options = _option_list()
    for model in GPT56_MODELS:
        assert model in options, f"{model} missing from codex_model_options"


def test_codex_model_options_have_no_bare_gpt56():
    # 裸 "gpt-5.6" 不是有效的 Codex 模型 ID（服务端只有 -sol/-terra/-luna）
    assert "gpt-5.6" not in _option_list()


def test_gpt56_sol_terra_support_max_and_ultra():
    for model in ("gpt-5.6-sol", "gpt-5.6-terra"):
        efforts = supported_codex_efforts(model)
        assert "max" in efforts
        assert "ultra" in efforts


def test_gpt56_luna_supports_max_but_not_ultra():
    efforts = supported_codex_efforts("gpt-5.6-luna")
    assert "max" in efforts
    assert "ultra" not in efforts


def test_older_models_fall_back_to_base_efforts():
    assert supported_codex_efforts("gpt-5.5") == ["low", "medium", "high", "xhigh"]
    assert supported_codex_efforts("gpt-5.4-mini") == ["low", "medium", "high", "xhigh"]


def test_default_model_used_when_model_is_none_or_default():
    expected = supported_codex_efforts(settings.default_codex_model)
    assert supported_codex_efforts(None) == expected
    assert supported_codex_efforts("default") == expected


def test_clamp_passes_supported_effort_through():
    assert clamp_codex_effort("gpt-5.6-sol", "max") == "max"
    assert clamp_codex_effort("gpt-5.6-sol", "ultra") == "ultra"
    assert clamp_codex_effort("gpt-5.6-luna", "max") == "max"
    assert clamp_codex_effort("gpt-5.5", "xhigh") == "xhigh"


def test_clamp_lowers_unsupported_effort_to_model_max():
    # 旧行为是静默丢弃 max（不传 flag）；现在夹到该模型最高档
    assert clamp_codex_effort("gpt-5.5", "max") == "xhigh"
    assert clamp_codex_effort("gpt-5.5", "ultra") == "xhigh"
    assert clamp_codex_effort("gpt-5.6-luna", "ultra") == "max"


def test_clamp_handles_none_and_unknown():
    assert clamp_codex_effort("gpt-5.6-sol", None) is None
    assert clamp_codex_effort("gpt-5.6-sol", "bogus") is None


def test_effort_map_keys_are_valid_model_options():
    options = _option_list()
    for model in CODEX_MODEL_EFFORTS:
        assert model in options
