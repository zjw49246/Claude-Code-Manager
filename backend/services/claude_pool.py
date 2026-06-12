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
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit / auth-failure detection (narrow patterns to avoid false positives)
# ---------------------------------------------------------------------------

_RATE_LIMIT_RE = re.compile(
    # "hit your limit" / "hit your session limit" / "hit your weekly limit"...
    r"hit your (?:\w+ )?limit"
    r"|usage limit reached"
    r"|session limit reached"
    # "resets 5pm (America/...)" / "resets 5:50pm (UTC)" — 任意时区、可带分钟
    r"|resets \d{1,2}(?::\d{2})?\s*[ap]m"
    r"|organization has been disabled"
    r"|organization has disabled"
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
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_CACHE_TTL = 60  # seconds
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code 公开 client_id
# Cloudflare 拦默认 python UA（403 error 1010），必须用 CLI 形态的 UA
OAUTH_USER_AGENT = "claude-cli/2.1.0 (external, cli)"


class PoolAccount:
    __slots__ = ("id", "config_dir", "email", "role", "enabled")

    def __init__(self, data: dict):
        account_id = data.get("id") or data.get("name")
        if not account_id:
            raise ValueError("Pool account requires 'id' or 'name'")
        self.id: str = account_id
        self.config_dir: str = os.path.expandvars(os.path.expanduser(data["config_dir"]))
        self.email: str = data.get("email", "")
        self.role: str = data.get("role", "automation")
        self.enabled: bool = data.get("enabled", True)


class ClaudePool:
    """In-process account pool with cooldown tracking."""

    def __init__(self, config_path: str | Path | None = None, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS):
        if config_path:
            expanded = os.path.expandvars(os.path.expanduser(str(config_path)))
            self._config_path = Path(expanded)
        else:
            self._config_path = DEFAULT_CONFIG_PATH
        self._cooldown_seconds = cooldown_seconds
        self._accounts: list[PoolAccount] = []
        # account_id -> timestamp when cooldown expires
        self._cooldowns: dict[str, float] = {}
        # Manual switch: preferred account is tried first by select(); if it's
        # cooled down / excluded / fails the probe, selection falls back to
        # the normal rotation order (auto rotation stays the safety net).
        self._preferred_account_id: str | None = None
        # Most recently selected account (display only — selection happens
        # per-launch, there is no persistent "current" account)
        self._last_selected_id: str | None = None
        self._last_selected_at: float = 0.0
        self._usage_cache: list[dict] | None = None
        self._usage_cache_at: float = 0.0
        # account_id -> asyncio.Lock，防并发重复 refresh（refresh token 会轮换）
        self._refresh_locks: dict[str, object] = {}
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
            "preferred": self._preferred_account_id,
            "last_selected": self._last_selected_id,
            "last_selected_at": self._last_selected_at or None,
            "accounts": accounts,
        }

    @property
    def preferred_account_id(self) -> str | None:
        return self._preferred_account_id

    def set_preferred(self, account_id: str | None) -> bool:
        """Pin an account as the preferred choice for subsequent launches.

        None clears the pin (back to pure auto rotation). Returns False if
        the account id is unknown.
        """
        if account_id is None:
            self._preferred_account_id = None
            logger.info("Pool preferred account cleared (auto rotation)")
            return True
        if not any(a.id == account_id for a in self._accounts):
            return False
        self._preferred_account_id = account_id
        logger.info("Pool preferred account set to %s", account_id)
        return True

    def select(self, *, exclude: set[str] | None = None, validate: bool = False) -> str | None:
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
        # Manual switch: preferred account jumps the queue; if it fails the
        # probe below the normal order takes over (auto-rotation fallback)
        if self._preferred_account_id:
            preferred = next(
                (a for a in candidates if a.id == self._preferred_account_id), None
            )
            if preferred:
                candidates.remove(preferred)
                candidates.insert(0, preferred)
        for chosen in candidates:
            if validate and not self._probe_account(chosen):
                continue
            logger.info("Pool selected account %s (%s)", chosen.id, chosen.config_dir)
            self._last_selected_id = chosen.id
            self._last_selected_at = time.time()
            return chosen.config_dir

        logger.warning("Pool has no healthy accounts after validation (exclude=%s)", exclude)
        return None

    async def select_async(self, *, exclude: set[str] | None = None, validate: bool = False) -> str | None:
        """Async wrapper for :meth:`select` — runs probe subprocesses in a thread
        so validation doesn't block the event loop (up to 30s per account)."""
        import asyncio
        return await asyncio.to_thread(self.select, exclude=exclude, validate=validate)

    def _probe_account(self, account: PoolAccount) -> bool:
        """Run a small Claude CLI probe before assigning work to an account."""
        # Same nested-session cleanup as InstanceManager.launch
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")}
        env["CLAUDE_CONFIG_DIR"] = account.config_dir
        try:
            proc = subprocess.run(
                ["claude", "-p", "reply ok only"],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Pool account %s probe timed out", account.id)
            self.mark_rate_limited(account.config_dir, duration=60)
            return False

        combined = "\n".join([proc.stdout or "", proc.stderr or ""]).strip()
        if proc.returncode == 0:
            return True
        if is_auth_failure(combined):
            self.mark_auth_failure(account.config_dir)
            return False
        if is_rate_limited(combined):
            self.mark_rate_limited(account.config_dir)
            return False
        logger.warning(
            "Pool account %s probe failed with non-rotatable output: %s",
            account.id,
            combined[:300],
        )
        return False

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

    def locate_session_config_dir(self, session_id: str, extra_dirs: list[str] | None = None) -> str | None:
        """Find which config dir actually holds the session JSONL.

        Searches all pool account dirs plus the env CLAUDE_CONFIG_DIR and the
        default ``~/.claude``, so session migration doesn't depend on callers
        knowing which account a session was created under.
        """
        candidates = [a.config_dir for a in self._accounts]
        env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if env_dir:
            candidates.append(env_dir)
        candidates.append(str(Path.home() / ".claude"))
        if extra_dirs:
            candidates.extend(extra_dirs)
        seen: set[str] = set()
        for d in candidates:
            d = os.path.expanduser(d)
            if d in seen:
                continue
            seen.add(d)
            try:
                if next(Path(d).glob(f"projects/*/{session_id}.jsonl"), None):
                    return d
            except OSError:
                continue
        return None

    def account(self, account_id: str) -> "PoolAccount | None":
        return next((a for a in self._accounts if a.id == account_id), None)

    async def refresh_oauth_token(self, account_id: str) -> bool:
        """手动触发某账号的 OAuth refresh（重新登录按钮的第一步）。

        成功后清掉 usage 缓存，让前端下次拉取立即看到恢复。
        """
        acc = self.account(account_id)
        if acc is None:
            raise KeyError(account_id)
        creds = await self._refresh_oauth(acc, Path(acc.config_dir) / ".credentials.json")
        if creds is None:
            return False
        self._usage_cache = None
        return True

    async def _refresh_oauth(self, account: "PoolAccount", cred_path: Path) -> dict | None:
        """accessToken 过期时用 refreshToken 换新（与 Claude CLI 自动刷新行为一致）。

        过期 ≠ 需要重新登录：CLI 平时跑着就会自己刷，闲置账号才会看到过期。
        成功：原子写回 .credentials.json（refresh token 会轮换，必须立即持久化）
        并返回新 creds；失败/无 refreshToken：返回 None——此时才真正需要重新登录。
        """
        import asyncio
        import tempfile

        import httpx

        lock = self._refresh_locks.setdefault(account.id, asyncio.Lock())
        async with lock:  # type: ignore[attr-defined]
            try:
                full = json.loads(cred_path.read_text(encoding="utf-8"))
                creds = full["claudeAiOauth"]
            except (OSError, ValueError, KeyError):
                return None
            # 等锁期间可能已被并发请求（或 CLI 进程自己）刷新过
            if creds.get("expiresAt", 0) / 1000 > time.time() + 60:
                return creds
            refresh_token = creds.get("refreshToken")
            if not refresh_token:
                return None
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(OAUTH_TOKEN_URL, json={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": OAUTH_CLIENT_ID,
                    }, headers={"User-Agent": OAUTH_USER_AGENT})
            except httpx.HTTPError as exc:
                logger.warning("pool %s: token refresh request failed: %s", account.id, exc)
                return None
            if resp.status_code != 200:
                logger.warning("pool %s: token refresh got HTTP %s", account.id, resp.status_code)
                return None
            data = resp.json()
            creds["accessToken"] = data["access_token"]
            if data.get("refresh_token"):
                creds["refreshToken"] = data["refresh_token"]
            creds["expiresAt"] = int((time.time() + data.get("expires_in", 28800)) * 1000)
            try:
                fd, tmp = tempfile.mkstemp(dir=str(cred_path.parent), prefix=".credentials.")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(full, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, cred_path)
            except OSError as exc:
                logger.error("pool %s: refreshed token write-back failed: %s", account.id, exc)
            logger.info("pool %s: OAuth token refreshed", account.id)
            return creds

    async def fetch_usage(self) -> list[dict]:
        """Per-account quota utilization from the Anthropic OAuth usage API.

        Reads each account's OAuth access token from
        ``<config_dir>/.credentials.json`` and queries the usage endpoint.
        Results are cached for USAGE_CACHE_TTL seconds.
        """
        import asyncio

        import httpx

        now = time.time()
        if self._usage_cache is not None and now - self._usage_cache_at < USAGE_CACHE_TTL:
            return self._usage_cache

        async def fetch_one(account: PoolAccount) -> dict:
            base = {"id": account.id, "email": account.email, "enabled": account.enabled,
                    "subscription_type": None, "error": None, "usage": None}
            cred_path = Path(account.config_dir) / ".credentials.json"
            try:
                creds = json.loads(cred_path.read_text(encoding="utf-8"))["claudeAiOauth"]
            except (OSError, ValueError, KeyError):
                base["error"] = "no_credentials"
                return base
            base["subscription_type"] = creds.get("subscriptionType")
            if creds.get("expiresAt", 0) / 1000 < now:
                # 先尝试 refresh——过期不等于要重新登录，刷不动才是
                creds = await self._refresh_oauth(account, cred_path)
                if creds is None:
                    base["error"] = "token_expired"
                    return base
                base["subscription_type"] = creds.get("subscriptionType") or base["subscription_type"]
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(USAGE_API_URL, headers={
                        "Authorization": f"Bearer {creds['accessToken']}",
                        "anthropic-beta": "oauth-2025-04-20",
                    })
            except httpx.HTTPError as exc:
                base["error"] = f"request_failed: {exc}"[:200]
                return base
            if resp.status_code != 200:
                base["error"] = f"http_{resp.status_code}"
                return base
            data = resp.json()

            def window(w: dict | None) -> dict | None:
                if not w:
                    return None
                return {"utilization": w.get("utilization"), "resets_at": w.get("resets_at")}

            base["usage"] = {
                "five_hour": window(data.get("five_hour")),
                "seven_day": window(data.get("seven_day")),
                "seven_day_opus": window(data.get("seven_day_opus")),
                "seven_day_sonnet": window(data.get("seven_day_sonnet")),
            }
            return base

        results = await asyncio.gather(*(fetch_one(a) for a in self._accounts))
        self._usage_cache = list(results)
        self._usage_cache_at = now
        return self._usage_cache


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
