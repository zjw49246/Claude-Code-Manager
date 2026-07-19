"""Codex model catalog: per-model reasoning effort support.

Source of truth: Codex CLI 服务端模型列表（~/.codex/models_cache.json, 2026-07-19 实测）。
GPT-5.6 是一个家族、三个模型（无裸 "gpt-5.6" ID）：
  - gpt-5.6-sol   (GPT-5.6 Sol,   frontier)  efforts: low..max + ultra
  - gpt-5.6-terra (GPT-5.6 Terra, balanced)  efforts: low..max + ultra
  - gpt-5.6-luna  (GPT-5.6 Luna,  fast)      efforts: low..max
旧模型（gpt-5.5 及更早）只支持 low..xhigh。
"""

from backend.config import settings

# 档位从低到高的全序，用于把不支持的高档位向下夹到该模型的最高档
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"]

# 基线档位：codex_effort_options（gpt-5.5 及更早的模型）
CODEX_MODEL_EFFORTS: dict[str, list[str]] = {
    "gpt-5.6-sol": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-5.6-terra": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"],
}


# context_window per model（~/.codex/models_cache.json 实测，2026-07-19：
# gpt-5.6-* / gpt-5.5 / gpt-5.4 / gpt-5.4-mini 均 272000，gpt-5.3-codex-spark 128000）
CODEX_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.6-sol": 272_000,
    "gpt-5.6-terra": 272_000,
    "gpt-5.6-luna": 272_000,
    "gpt-5.5": 272_000,
    "gpt-5.4": 272_000,
    "gpt-5.4-mini": 272_000,
    "gpt-5.3-codex-spark": 128_000,
}
DEFAULT_CODEX_CONTEXT_WINDOW = 272_000


def codex_context_window(model: str | None) -> int:
    """Context window for a codex model (falls back to the family default)."""
    if not model or model == "default":
        model = settings.default_codex_model
    return CODEX_CONTEXT_WINDOWS.get(model, DEFAULT_CODEX_CONTEXT_WINDOW)


def base_codex_efforts() -> list[str]:
    return [e.strip() for e in settings.codex_effort_options.split(",") if e.strip()]


def supported_codex_efforts(model: str | None) -> list[str]:
    """Effort levels supported by the given codex model (falls back to the base list)."""
    if not model or model == "default":
        model = settings.default_codex_model
    return CODEX_MODEL_EFFORTS.get(model, base_codex_efforts())


def clamp_codex_effort(model: str | None, effort: str | None) -> str | None:
    """Clamp an effort level to what the model supports.

    Supported efforts pass through; unsupported ones clamp to the model's
    highest supported level (e.g. "max" on gpt-5.5 → "xhigh") instead of the
    legacy behavior of silently dropping the flag. Unknown effort strings
    return None so the CLI default applies.
    """
    if not effort:
        return None
    supported = supported_codex_efforts(model)
    if effort in supported:
        return effort
    if effort not in EFFORT_ORDER:
        return None
    # 向下夹到该模型支持的最高档
    idx = EFFORT_ORDER.index(effort)
    for lower in reversed(EFFORT_ORDER[:idx]):
        if lower in supported:
            return lower
    return None
