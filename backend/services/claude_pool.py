"""Claude account pool — automatic rotation on rate limit / auth failure.

Reads account configuration from ``~/.claude-pool/accounts.json`` (compatible
with the agent-ml-research pool format). Each account has its own
``CLAUDE_CONFIG_DIR`` so Claude Code sees independent OAuth credentials.

When a subprocess hits a rate limit or auth failure, the dispatcher calls
:func:`select` to pick the next available account and :func:`migrate_session`
to hardlink the session JSONL so ``--resume`` works transparently.
"""

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit / auth-failure detection (narrow patterns to avoid false positives)
# ---------------------------------------------------------------------------

_RATE_LIMIT_RE = re.compile(
    r"hit your limit"
    r"|usage limit reached"
    r"|resets \d{1,2}[ap]m \(America/"
    r"|organization has been disabled"
    r"|account has been disabled"
    r"|当前限速",
    re.IGNORECASE,
)

_AUTH_FAIL_RE = re.compile(
    r"not logged in"
    r"|please run /login"
    r"|not authenticated"
    r"|please log in"
    r"|failed to authenticate",
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


def is_pool_rotatable(text: str) -> bool:
    """Return True if the output warrants trying another pool account."""
    return is_rate_limited(text) or is_auth_failure(text)


# ---------------------------------------------------------------------------
# Account configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".claude-pool" / "accounts.json"
DEFAULT_COOLDOWN_SECONDS = 300  # 5 minutes


class PoolAccount:
    __slots__ = ("id", "config_dir", "email", "role", "enabled")

    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.config_dir: str = os.path.expandvars(os.path.expanduser(data["config_dir"]))
        self.email: str = data.get("email", "")
        self.role: str = data.get("role", "automation")
        self.enabled: bool = data.get("enabled", True)


class ClaudePool:
    """In-process account pool with cooldown tracking."""

    def __init__(self, config_path: str | Path | None = None, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS):
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._cooldown_seconds = cooldown_seconds
        self._accounts: list[PoolAccount] = []
        # account_id -> timestamp when cooldown expires
        self._cooldowns: dict[str, float] = {}
        self._load()

    @property
    def enabled(self) -> bool:
        return len(self._accounts) > 1

    def _load(self):
        if not self._config_path.exists():
            logger.info("Pool config not found at %s, pool disabled", self._config_path)
            return
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            self._accounts = [PoolAccount(a) for a in data.get("accounts", [])]
            logger.info("Pool loaded %d accounts from %s", len(self._accounts), self._config_path)
        except Exception:
            logger.exception("Failed to load pool config from %s", self._config_path)

    def reload(self):
        self._accounts.clear()
        self._load()

    def list_accounts(self) -> list[dict]:
        now = time.time()
        result = []
        for a in self._accounts:
            cd_until = self._cooldowns.get(a.id, 0)
            result.append({
                "id": a.id,
                "config_dir": a.config_dir,
                "email": a.email,
                "role": a.role,
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
            "accounts": accounts,
        }

    def select(self, *, exclude: set[str] | None = None) -> str | None:
        """Pick the best available account config_dir, excluding specified IDs.

        Returns the config_dir path, or None if no account is available.
        """
        now = time.time()
        candidates = []
        for a in self._accounts:
            if not a.enabled:
                continue
            if exclude and a.id in exclude:
                continue
            if now < self._cooldowns.get(a.id, 0):
                continue
            candidates.append(a)

        if not candidates:
            logger.warning("Pool has no available accounts (exclude=%s)", exclude)
            return None

        # Simple round-robin: pick the one whose cooldown expired earliest
        # (or never had one), which naturally distributes load
        candidates.sort(key=lambda a: self._cooldowns.get(a.id, 0))
        chosen = candidates[0]
        logger.info("Pool selected account %s (%s)", chosen.id, chosen.config_dir)
        return chosen.config_dir

    def account_id_from_config_dir(self, config_dir: str) -> str | None:
        for a in self._accounts:
            if a.config_dir == config_dir:
                return a.id
        return None

    def mark_rate_limited(self, config_dir: str, duration: int | None = None):
        """Mark an account as rate-limited with a cooldown period."""
        account_id = self.account_id_from_config_dir(config_dir)
        if not account_id:
            logger.warning("Cannot mark unknown config_dir as rate-limited: %s", config_dir)
            return
        cd = duration or self._cooldown_seconds
        self._cooldowns[account_id] = time.time() + cd
        logger.info("Pool account %s rate-limited for %ds", account_id, cd)

    def mark_auth_failure(self, config_dir: str):
        """Mark an account with auth failure — indefinite cooldown until manual clear."""
        account_id = self.account_id_from_config_dir(config_dir)
        if not account_id:
            return
        # Far future = effectively permanent until cleared
        self._cooldowns[account_id] = time.time() + 86400 * 365
        logger.warning("Pool account %s marked auth-failure (indefinite cooldown)", account_id)

    def clear_cooldown(self, account_id: str):
        self._cooldowns.pop(account_id, None)
        logger.info("Pool cooldown cleared for account %s", account_id)


# ---------------------------------------------------------------------------
# Session migration (hardlink JSONL for --resume across accounts)
# ---------------------------------------------------------------------------

def migrate_session(
    *,
    old_config_dir: str,
    new_config_dir: str,
    session_id: str,
) -> bool:
    """Hardlink a Claude session JSONL from old_config_dir to new_config_dir.

    Claude stores session history at
    ``<CLAUDE_CONFIG_DIR>/projects/<encoded_cwd>/<session_id>.jsonl``.
    ``--resume`` only looks under the *current* CLAUDE_CONFIG_DIR, so we
    hardlink the file so it's visible from both directories.

    Returns True if the file is in place after the call, False on failure.
    """
    old_root = Path(old_config_dir)
    new_root = Path(new_config_dir)
    try:
        candidates = list(old_root.glob(f"projects/*/{session_id}.jsonl"))
        if not candidates:
            logger.warning(
                "migrate_session: no jsonl for sid=%s under %s — context will be lost",
                session_id, old_root,
            )
            return False

        old_jsonl = candidates[0]
        encoded_cwd = old_jsonl.parent.name
        new_jsonl = new_root / "projects" / encoded_cwd / f"{session_id}.jsonl"

        if new_jsonl.exists():
            try:
                if new_jsonl.stat().st_ino == old_jsonl.stat().st_ino:
                    return True  # already hardlinked
            except OSError:
                return False
            logger.warning(
                "migrate_session: %s already exists under %s with different inode",
                session_id, new_root,
            )
            return False

        new_jsonl.parent.mkdir(parents=True, exist_ok=True)
        os.link(old_jsonl, new_jsonl)
        logger.info("migrate_session: hardlinked %s → %s", old_jsonl, new_jsonl)
        return True
    except OSError as exc:
        logger.warning("migrate_session(%s → %s, sid=%s): %s", old_config_dir, new_config_dir, session_id, exc)
        return False


# ---------------------------------------------------------------------------
# Collect rate-limit text from stderr + last N log entries
# ---------------------------------------------------------------------------

def collect_process_output_for_detection(stderr: str, last_log_contents: list[str]) -> str:
    """Combine stderr and recent log entry contents for rate-limit detection."""
    parts = []
    if stderr:
        parts.append(stderr)
    parts.extend(c for c in last_log_contents if c)
    return "\n".join(parts)
