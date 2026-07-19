"""Codex account pool — multi-account rotation and quota tracking.

Parallel to claude_pool.py but for OpenAI Codex CLI accounts.
Config: ~/.codex-pool/accounts.json
Each account has its own CODEX_HOME directory with auth.json.

Quota is read from Codex session rollout files:
  CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl
which contain rate_limits payloads after each turn.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit / auth-failure detection
# ---------------------------------------------------------------------------

_RATE_LIMIT_RE = re.compile(
    r"\brate[ _-]?limit(?:ed|ing)?\b"
    r"|\bquota\s+exceeded\b"
    r"|insufficient_quota"
    r"|exceeded\s+your\s+(?:current\s+)?quota"
    r"|(?:HTTP\s*)?429\s*(?:too\s+many|rate)"
    r"|too\s+many\s+requests"
    r"|spend\s+cap",
    re.IGNORECASE,
)

_AUTH_FAIL_RE = re.compile(
    r"(?:HTTP\s*)?401\s+unauthorized"
    r"|invalid\s+api[ _-]?key"
    r"|not\s+logged\s+in"
    r"|please\s+run\s+`?codex\s+login`?"
    r"|token\s+has\s+been\s+invalidated"
    r"|refresh\s+token\s+was\s+revoked",
    re.IGNORECASE,
)

_TRANSIENT_RE = re.compile(
    r"stream\s+disconnected"
    r"|connection\s+(?:failed|reset|refused)"
    r"|(?:HTTP\s*)?5\d{2}\s+(?:service|internal|bad gateway)"
    r"|service\s+unavailable"
    r"|\boverloaded(?:_error)?\b",
    re.IGNORECASE,
)


def is_rate_limited(text: str) -> bool:
    if not text:
        return False
    return bool(_RATE_LIMIT_RE.search(text))


def is_auth_failure(text: str) -> bool:
    if not text:
        return False
    return bool(_AUTH_FAIL_RE.search(text))


def is_transient(text: str) -> bool:
    if not text:
        return False
    if is_rate_limited(text) or is_auth_failure(text):
        return False
    return bool(_TRANSIENT_RE.search(text))


def is_pool_rotatable(text: str) -> bool:
    return is_rate_limited(text) or is_auth_failure(text)


# ---------------------------------------------------------------------------
# Account configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".codex-pool" / "accounts.json"
DEFAULT_COOLDOWN_SECONDS = 300
QUOTA_CACHE_TTL = 120  # seconds


class CodexPoolAccount:
    __slots__ = ("id", "codex_home", "email", "enabled")

    def __init__(self, data: dict):
        self.id: str = data.get("id") or data.get("name") or ""
        if not self.id:
            raise ValueError("Codex pool account requires 'id'")
        self.codex_home: str = os.path.expandvars(os.path.expanduser(data["codex_home"]))
        self.email: str = data.get("email", "")
        self.enabled: bool = data.get("enabled", True)


class CodexPool:
    """In-process Codex account pool with cooldown and quota tracking."""

    def __init__(self, config_path: str | Path | None = None, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS):
        if config_path:
            self._config_path = Path(os.path.expandvars(os.path.expanduser(str(config_path))))
        else:
            self._config_path = DEFAULT_CONFIG_PATH
        self._cooldown_seconds = cooldown_seconds
        self._accounts: list[CodexPoolAccount] = []
        self._cooldowns: dict[str, float] = {}
        self._preferred_account_id: str | None = None
        self._last_selected_id: str | None = None
        self._last_selected_at: float = 0.0
        self._quota_cache: dict[str, dict] | None = None
        self._quota_cache_at: float = 0.0
        self._load()

    @property
    def enabled(self) -> bool:
        return True

    def _load(self):
        if not self._config_path.exists():
            self._bootstrap_default()
            if not self._config_path.exists():
                logger.info("Codex pool config not found at %s", self._config_path)
                return
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            self._accounts = [CodexPoolAccount(a) for a in data.get("accounts", [])]
            logger.info("Codex pool loaded %d accounts from %s", len(self._accounts), self._config_path)
        except Exception:
            logger.exception("Failed to load codex pool config")

    def _bootstrap_default(self):
        """If no pool config exists but ~/.codex/auth.json does, bootstrap it."""
        default_auth = Path.home() / ".codex" / "auth.json"
        if not default_auth.exists():
            return
        try:
            auth = json.loads(default_auth.read_text())
            tokens = auth.get("tokens") or {}
            if not tokens.get("access_token"):
                return
        except Exception:
            return
        # Try to get email from id_token JWT
        email = _extract_email_from_jwt(tokens.get("id_token", ""))
        data = {"accounts": [{
            "id": "codex-1",
            "codex_home": str(Path.home() / ".codex"),
            "email": email or "default",
            "enabled": True,
        }]}
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("Bootstrapped default codex account (%s) into pool", email)

    def reload(self):
        self._accounts.clear()
        self._load()

    def account(self, account_id: str) -> CodexPoolAccount | None:
        return next((a for a in self._accounts if a.id == account_id), None)

    def list_accounts(self) -> list[dict]:
        now = time.time()
        result = []
        for a in self._accounts:
            cd_until = self._cooldowns.get(a.id, 0)
            result.append({
                "id": a.id,
                "codex_home": a.codex_home,
                "email": a.email,
                "enabled": a.enabled,
                "available": a.enabled and now >= cd_until,
                "cooldown_until": cd_until if cd_until > now else None,
                "cooldown_remaining": max(0, cd_until - now) if cd_until > now else 0,
            })
        return result

    def status(self) -> dict:
        accounts = self.list_accounts()
        return {
            "enabled": self.enabled,
            "total": len(accounts),
            "available": sum(1 for a in accounts if a["available"]),
            "cooldown": sum(1 for a in accounts if not a["available"] and a["enabled"]),
            "disabled": sum(1 for a in accounts if not a["enabled"]),
            "preferred": self._preferred_account_id,
            "last_selected": self._last_selected_id,
            "last_selected_at": self._last_selected_at or None,
            "accounts": accounts,
        }

    @property
    def preferred_account_id(self) -> str | None:
        return self._preferred_account_id

    def set_preferred(self, account_id: str | None) -> bool:
        if account_id is None:
            self._preferred_account_id = None
            return True
        if not any(a.id == account_id for a in self._accounts):
            return False
        self._preferred_account_id = account_id
        return True

    def select(self, exclude: set[str] | None = None) -> str | None:
        """Pick an available CODEX_HOME. Returns None if all exhausted."""
        now = time.time()
        excluded = exclude or set()
        candidates = [
            a for a in self._accounts
            if a.enabled and a.id not in excluded and now >= self._cooldowns.get(a.id, 0)
        ]
        if not candidates:
            return None

        # Prefer the pinned account if available
        if self._preferred_account_id:
            preferred = next((a for a in candidates if a.id == self._preferred_account_id), None)
            if preferred:
                self._last_selected_id = preferred.id
                self._last_selected_at = now
                return preferred.codex_home

        # Round-robin: pick the one not selected most recently
        chosen = candidates[0]
        self._last_selected_id = chosen.id
        self._last_selected_at = now
        return chosen.codex_home

    def mark_rate_limited(self, codex_home: str, duration: int | None = None):
        acc = self._find_by_home(codex_home)
        if acc:
            d = duration if duration is not None else self._cooldown_seconds
            self._cooldowns[acc.id] = time.time() + d
            logger.info("Codex pool: marked %s rate-limited for %ds", acc.id, d)

    def mark_auth_failure(self, codex_home: str):
        acc = self._find_by_home(codex_home)
        if acc:
            self._cooldowns[acc.id] = time.time() + 365 * 86400
            logger.info("Codex pool: marked %s auth-failed (indefinite)", acc.id)

    def is_in_cooldown(self, codex_home: str) -> bool:
        acc = self._find_by_home(codex_home)
        if not acc:
            return False
        return time.time() < self._cooldowns.get(acc.id, 0)

    def clear_cooldown(self, account_id: str):
        self._cooldowns.pop(account_id, None)

    def _find_by_home(self, codex_home: str) -> CodexPoolAccount | None:
        target = str(Path(codex_home).expanduser()).rstrip("/")
        for a in self._accounts:
            if str(Path(a.codex_home).expanduser()).rstrip("/") == target:
                return a
        return None

    # --- Quota tracking (from rollout files) ---

    async def fetch_quota(self, force: bool = False) -> list[dict]:
        """Read quota from each account's latest rollout file."""
        now = time.time()
        if not force and self._quota_cache is not None and (now - self._quota_cache_at) < QUOTA_CACHE_TTL:
            return list(self._quota_cache.values())

        results = {}
        for acc in self._accounts:
            if not acc.enabled:
                continue
            quota = _read_quota_from_rollout(acc.codex_home)
            results[acc.id] = {
                "id": acc.id,
                "email": acc.email,
                "codex_home": acc.codex_home,
                "plan_type": quota.get("plan_type") if quota else None,
                "quota": quota,
                "error": None if quota else "no_rollout_data",
            }

        self._quota_cache = results
        self._quota_cache_at = now
        return list(results.values())


# ---------------------------------------------------------------------------
# Quota helpers
# ---------------------------------------------------------------------------

def _read_quota_from_rollout(codex_home: str) -> dict | None:
    """Parse the newest rollout file for rate_limits data."""
    sessions_dir = Path(codex_home) / "sessions"
    if not sessions_dir.is_dir():
        return None

    candidates = list(sessions_dir.glob("*/*/*/rollout-*.jsonl"))
    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)

    found = None
    try:
        with newest.open() as f:
            for line in f:
                line = line.strip()
                if not line or '"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                rate_limits = payload.get("rate_limits")
                if isinstance(rate_limits, dict):
                    found = rate_limits
    except OSError:
        return None

    if not found:
        return None

    primary = found.get("primary") or {}
    secondary = found.get("secondary") or {}
    return {
        "primary_used_percent": primary.get("used_percent"),
        "primary_window_minutes": primary.get("window_minutes"),
        "primary_resets_at": primary.get("resets_at"),
        "secondary_used_percent": secondary.get("used_percent") if secondary else None,
        "secondary_window_minutes": secondary.get("window_minutes") if secondary else None,
        "secondary_resets_at": secondary.get("resets_at") if secondary else None,
        "plan_type": found.get("plan_type"),
        "is_rate_limited": found.get("rate_limit_reached_type") is not None,
        "has_credits": (found.get("credits") or {}).get("has_credits", False),
    }


def _extract_email_from_jwt(id_token: str) -> str:
    """Extract email from JWT id_token payload (no verification)."""
    if not id_token:
        return ""
    try:
        import base64
        parts = id_token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        return data.get("email", "")
    except Exception:
        return ""


def verify_login(codex_home: str) -> dict:
    """Check if the account at codex_home has valid auth.json."""
    auth_path = Path(codex_home) / "auth.json"
    if not auth_path.exists():
        return {"logged_in": False, "detail": "auth.json missing"}
    try:
        data = json.loads(auth_path.read_text())
    except Exception:
        return {"logged_in": False, "detail": "auth.json unreadable"}

    tokens = data.get("tokens") or {}
    has_access = bool(tokens.get("access_token") or data.get("OPENAI_API_KEY"))
    email = _extract_email_from_jwt(tokens.get("id_token", ""))

    # Check subscription info from id_token
    plan_type = None
    subscription_until = None
    try:
        import base64
        parts = (tokens.get("id_token") or "").split(".")
        if len(parts) >= 2:
            payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            auth_info = claims.get("https://api.openai.com/auth", {})
            plan_type = auth_info.get("chatgpt_plan_type")
            subscription_until = auth_info.get("chatgpt_subscription_active_until")
    except Exception:
        pass

    return {
        "logged_in": has_access,
        "email": email,
        "plan_type": plan_type,
        "subscription_until": subscription_until,
        "auth_mode": data.get("auth_mode"),
        "detail": "ok" if has_access else "no access token",
    }
