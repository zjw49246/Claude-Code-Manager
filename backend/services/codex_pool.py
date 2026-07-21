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
import time
from pathlib import Path

from backend.services.claude_pool import (
    is_codex_auth_failure,
    is_codex_transient,
    is_codex_usage_limited,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit / auth-failure detection
# ---------------------------------------------------------------------------

def is_rate_limited(text: str) -> bool:
    """Backward-compatible alias for the shared Codex usage-limit detector."""
    return is_codex_usage_limited(text)


def is_auth_failure(text: str) -> bool:
    """Backward-compatible alias for the shared Codex auth detector."""
    return is_codex_auth_failure(text)


def is_transient(text: str) -> bool:
    """Backward-compatible alias for the shared Codex transient detector."""
    return is_codex_transient(text)


def is_pool_rotatable(text: str) -> bool:
    return is_rate_limited(text) or is_auth_failure(text)


# ---------------------------------------------------------------------------
# Account configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".codex-pool" / "accounts.json"
DEFAULT_COOLDOWN_SECONDS = 300
QUOTA_CACHE_TTL = 120  # seconds
QUOTA_SWITCH_THRESHOLD_PERCENT = 90.0
PROACTIVE_QUOTA_MAX_COOLDOWN_SECONDS = 8 * 24 * 60 * 60
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def quota_at_or_above(
    quota: dict | None, *, threshold: float = QUOTA_SWITCH_THRESHOLD_PERCENT
) -> bool:
    """Whether Codex's 5-hour or weekly window reached ``threshold`` percent."""

    if not isinstance(quota, dict):
        return False
    for key in ("primary_used_percent", "secondary_used_percent"):
        try:
            if float(quota.get(key)) >= threshold:
                return True
        except (TypeError, ValueError):
            continue
    return False


def quota_cooldown_seconds(
    quota: dict | None,
    *,
    threshold: float = QUOTA_SWITCH_THRESHOLD_PERCENT,
    now: float | None = None,
    fallback: int = DEFAULT_COOLDOWN_SECONDS,
    maximum: int = PROACTIVE_QUOTA_MAX_COOLDOWN_SECONDS,
) -> int:
    """Return cooldown through the latest reset of every high quota window.

    Codex reports Unix timestamps for the primary (5-hour) and secondary
    (weekly) windows. Only windows whose usage reached ``threshold`` count; if
    both are high, the later reset keeps the old account out of selection until
    both windows are usable again. Millisecond timestamps are accepted for
    defensive compatibility with upstream payload changes.
    """

    if not isinstance(quota, dict):
        return max(1, int(fallback))

    reset_timestamps: list[float] = []
    for used_key, reset_key in (
        ("primary_used_percent", "primary_resets_at"),
        ("secondary_used_percent", "secondary_resets_at"),
    ):
        try:
            if float(quota.get(used_key)) < threshold:
                continue
            reset_at = float(quota.get(reset_key))
            if reset_at > 10_000_000_000:  # milliseconds, not seconds
                reset_at /= 1000
            reset_timestamps.append(reset_at)
        except (TypeError, ValueError):
            continue

    current = time.time() if now is None else now
    future_resets = [
        reset_at for reset_at in reset_timestamps if reset_at > current
    ]
    if not future_resets:
        return max(1, int(fallback))
    remaining = int(max(future_resets) - current)
    return min(max(1, remaining), max(1, int(maximum)))


class AmbiguousCodexSessionHomeError(RuntimeError):
    """A Codex thread rollout exists under more than one account home."""

    def __init__(self, session_id: str, homes: list[str]):
        self.session_id = session_id
        self.homes = homes
        super().__init__(
            f"Codex session {session_id!r} exists in multiple homes: "
            + ", ".join(homes)
        )


def canonical_codex_home(codex_home: str | os.PathLike[str]) -> str:
    """Return the stable absolute identity for a CODEX_HOME directory.

    Account lookup, app-server routing, cooldown state, and session ownership
    must all compare the same value.  Resolving existing symlinks also prevents
    one credential directory from being registered under two spellings.
    """

    raw = os.path.expandvars(os.path.expanduser(os.fspath(codex_home)))
    if not raw:
        raise ValueError("CODEX_HOME cannot be empty")
    return str(Path(raw).resolve(strict=False))


class CodexPoolAccount:
    __slots__ = (
        "id", "codex_home", "email", "enabled", "retired", "cleanup_pending",
    )

    def __init__(self, data: dict):
        self.id: str = data.get("id") or data.get("name") or ""
        if not self.id:
            raise ValueError("Codex pool account requires 'id'")
        self.codex_home: str = canonical_codex_home(data["codex_home"])
        self.email: str = str(data.get("email") or "")
        self.retired: bool = bool(data.get("retired", False))
        self.cleanup_pending: bool = bool(data.get("cleanup_pending", False))
        self.enabled: bool = bool(data.get("enabled", True)) and not self.retired


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
            accounts = [CodexPoolAccount(a) for a in data.get("accounts", [])]
            account_ids = [account.id for account in accounts]
            account_homes = [account.codex_home for account in accounts]
            if len(account_ids) != len(set(account_ids)):
                raise ValueError("Codex pool account ids must be unique")
            if len(account_homes) != len(set(account_homes)):
                raise ValueError(
                    "Each Codex pool account must use a distinct CODEX_HOME"
                )
            self._accounts = accounts
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

        valid_ids = {
            account.id for account in self._accounts if not account.retired
        }
        self._cooldowns = {
            account_id: until
            for account_id, until in self._cooldowns.items()
            if account_id in valid_ids
        }
        if self._preferred_account_id not in valid_ids:
            self._preferred_account_id = None
        if self._last_selected_id not in valid_ids:
            self._last_selected_id = None
            self._last_selected_at = 0.0
        # Account membership/home changes invalidate every quota entry, even
        # when the same account id remains in the reloaded file.
        self._quota_cache = None
        self._quota_cache_at = 0.0

    def account(self, account_id: str) -> CodexPoolAccount | None:
        return next((a for a in self._accounts if a.id == account_id), None)

    @staticmethod
    def canonical_home(codex_home: str | os.PathLike[str]) -> str:
        return canonical_codex_home(codex_home)

    def account_for_home(
        self, codex_home: str | os.PathLike[str]
    ) -> CodexPoolAccount | None:
        """Return the registered account owning ``codex_home``."""

        try:
            target = canonical_codex_home(codex_home)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        return next((a for a in self._accounts if a.codex_home == target), None)

    def account_id_for_home(self, codex_home: str | os.PathLike[str]) -> str | None:
        account = self.account_for_home(codex_home)
        return account.id if account else None

    # Explicit spelling for call sites where several provider pools coexist.
    account_id_from_codex_home = account_id_for_home

    def home_for_account(self, account_id: str) -> str | None:
        account = self.account(account_id)
        return account.codex_home if account else None

    def account_status(self, account_id: str) -> dict | None:
        """Return current enabled/cooldown state for one account id."""

        account = self.account(account_id)
        if not account:
            return None
        now = time.time()
        cooldown_until = self._cooldowns.get(account.id, 0)
        return {
            "id": account.id,
            "codex_home": account.codex_home,
            "email": account.email,
            "enabled": account.enabled,
            "retired": account.retired,
            "available": account.enabled and now >= cooldown_until,
            "cooldown_until": cooldown_until if cooldown_until > now else None,
            "cooldown_remaining": (
                max(0, cooldown_until - now) if cooldown_until > now else 0
            ),
        }

    def home_status(self, codex_home: str | os.PathLike[str]) -> dict | None:
        account = self.account_for_home(codex_home)
        return self.account_status(account.id) if account else None

    def is_home_enabled(self, codex_home: str | os.PathLike[str]) -> bool:
        account = self.account_for_home(codex_home)
        return bool(account and account.enabled)

    def is_home_available(self, codex_home: str | os.PathLike[str]) -> bool:
        state = self.home_status(codex_home)
        return bool(state and state["available"])

    def is_disabled(self, codex_home: str | os.PathLike[str]) -> bool:
        """Whether a known account home is explicitly disabled."""

        account = self.account_for_home(codex_home)
        return account is not None and not account.enabled

    def is_known_account(self, codex_home: str | os.PathLike[str]) -> bool:
        return self.account_for_home(codex_home) is not None

    def list_accounts(self) -> list[dict]:
        result: list[dict] = []
        for account in self._accounts:
            # Retired tombstones remain internally addressable so historical
            # task bindings can migrate their rollout, but they are deleted
            # from the user-facing pool and are never selectable.
            if account.retired:
                continue
            state = self.account_status(account.id)
            if state is not None:
                result.append(state)
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
        if not any(
            a.id == account_id and not a.retired for a in self._accounts
        ):
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
                return self._record_selection(preferred, now)

        # True round-robin follows config order and resumes immediately after
        # the previously selected id, skipping excluded/disabled/cooled homes.
        candidate_ids = {account.id for account in candidates}
        start = 0
        if self._last_selected_id:
            previous = next(
                (
                    index
                    for index, account in enumerate(self._accounts)
                    if account.id == self._last_selected_id
                ),
                None,
            )
            if previous is not None:
                start = (previous + 1) % len(self._accounts)
        for offset in range(len(self._accounts)):
            chosen = self._accounts[(start + offset) % len(self._accounts)]
            if chosen.id in candidate_ids:
                return self._record_selection(chosen, now)
        return None

    def _record_selection(self, account: CodexPoolAccount, now: float) -> str:
        self._last_selected_id = account.id
        self._last_selected_at = now
        logger.info("Codex pool selected account %s (%s)", account.id, account.codex_home)
        return account.codex_home

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
        return self.account_for_home(codex_home)

    def _session_search_homes(self, extra_homes: list[str] | None = None) -> list[str]:
        candidates: list[str | os.PathLike[str]] = [
            account.codex_home for account in self._accounts
        ]
        env_home = os.environ.get("CODEX_HOME")
        if env_home:
            candidates.append(env_home)
        candidates.append(Path.home() / ".codex")
        if extra_homes:
            candidates.extend(extra_homes)

        # Include orphaned homes left by accounts removed from accounts.json.
        # Their rollout may be the only copy of a task's native thread.
        try:
            candidates.extend(
                path
                for path in sorted(Path.home().iterdir())
                if path.is_dir()
                and (path.name == ".codex" or path.name.startswith(".codex-"))
            )
        except OSError:
            pass

        homes: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                canonical = canonical_codex_home(candidate)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            homes.append(canonical)
        return homes

    def locate_session_homes(
        self,
        session_id: str,
        extra_homes: list[str] | None = None,
    ) -> list[str]:
        """Return every CODEX_HOME containing a rollout for ``session_id``.

        Multiple copies are expected after account migration.  Returning all
        homes lets the dispatcher use the task's account affinity to choose;
        this method never silently makes that ownership decision.
        """

        if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
            raise ValueError(
                "Invalid Codex session id; expected letters, digits, '.', '_' or '-'"
            )
        matches: list[str] = []
        pattern = f"*/*/*/rollout-*-{session_id}.jsonl"
        for home in self._session_search_homes(extra_homes):
            try:
                found = any(
                    rollout.is_file()
                    for rollout in (Path(home) / "sessions").glob(pattern)
                )
            except OSError:
                continue
            if found:
                matches.append(home)
        return matches

    def locate_session_home(
        self,
        session_id: str,
        extra_homes: list[str] | None = None,
    ) -> str | None:
        """Return the unique home holding a session, or raise on ambiguity."""

        homes = self.locate_session_homes(session_id, extra_homes=extra_homes)
        if len(homes) > 1:
            raise AmbiguousCodexSessionHomeError(session_id, homes)
        return homes[0] if homes else None

    # --- Quota tracking (from rollout files) ---

    async def fetch_quota(self, force: bool = False) -> list[dict]:
        """Read quota from each account's latest rollout file."""
        now = time.time()
        if not force and self._quota_cache is not None and (now - self._quota_cache_at) < QUOTA_CACHE_TTL:
            return list(self._quota_cache.values())

        results = {}
        enabled_accounts = [acc for acc in self._accounts if acc.enabled]
        quotas = await asyncio.gather(*(
            asyncio.to_thread(_read_quota_from_rollout, acc.codex_home)
            for acc in enabled_accounts
        ))
        for acc, quota in zip(enabled_accounts, quotas):
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

    async def select_quota_alternative(
        self,
        current_home: str,
        *,
        threshold: float = QUOTA_SWITCH_THRESHOLD_PERCENT,
    ) -> str | None:
        """Return a below-threshold alternative when the current home is high.

        Quota is refreshed from each account's latest rollout after a completed
        turn. Unknown quota remains eligible, while known-high, disabled, and
        cooled accounts cannot be chosen. No cooldown is written here, so a pool
        with no usable alternative simply continues on the current account.
        """

        current_id = self.account_id_for_home(current_home)
        if not current_id:
            return None
        quota_by_id = {
            row["id"]: row for row in await self.fetch_quota(force=True)
        }
        current = quota_by_id.get(current_id)
        if not current or not quota_at_or_above(
            current.get("quota"), threshold=threshold
        ):
            return None

        excluded = {current_id}
        alternatives = [
            account
            for account in self._accounts
            if account.enabled and account.id != current_id
        ]
        login_states = await asyncio.gather(
            *(
                asyncio.to_thread(verify_login, account.codex_home)
                for account in alternatives
            ),
            return_exceptions=True,
        )
        for account, login_state in zip(alternatives, login_states):
            if (
                isinstance(login_state, dict)
                and login_state.get("logged_in") is False
            ):
                excluded.add(account.id)

        for account in alternatives:
            row = quota_by_id.get(account.id)
            if row and quota_at_or_above(row.get("quota"), threshold=threshold):
                excluded.add(account.id)
        return self.select(exclude=excluded)

    def cached_quota_for_home(self, codex_home: str) -> dict | None:
        """Return the latest quota snapshot populated by ``fetch_quota``."""

        account_id = self.account_id_for_home(codex_home)
        if not account_id or not isinstance(self._quota_cache, dict):
            return None
        row = self._quota_cache.get(account_id)
        quota = row.get("quota") if isinstance(row, dict) else None
        return quota if isinstance(quota, dict) else None


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
