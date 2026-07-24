"""Codex (OpenAI) automated login — drives `codex login` OAuth flow via Playwright.

Flow:
1. Start `codex login` → it listens on 127.0.0.1:1455 and prints an OAuth authorize URL
2. Parse the authorize URL from stdout (strip ANSI codes)
3. Drive headful real-Chrome Playwright browser through OpenAI login pages:
   - Email field → fill email → Continue
   - Password field → fill password (if present; passwordless accounts skip)
   - OTP field → poll 171mail/MailCatcher when available, otherwise wait for a human code
   - Consent/Authorize button → click
4. Codex captures the callback, exchanges token, writes CODEX_HOME/auth.json
5. Smoke-test with `codex exec`

Prerequisites:
- Xvfb running on :99 (DISPLAY set)
- google-chrome-stable installed
- playwright installed
- MailCatcher-backed addresses (including 163/mail.com/Onet/Gazeta) use the
  query token issued by MailCatcher (never the mailbox password)
- codex CLI installed
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Constants ---

DEFAULT_LOGIN_TIMEOUT = 300
AUTH_URL_TIMEOUT = 30
AUTH_JSON_WAIT = 30
STATE_STEP_PAUSE_MS = 2500
MANUAL_OTP_TIMEOUT = 600
MAX_OTP_ATTEMPTS = 3
INPUT_INIT_TIMEOUT = 30
LOGIN_EVENT_PREFIX = "CCM_CODEX_LOGIN_EVENT:"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
AUTHORIZE_URL_RE = re.compile(r"(https://auth\.openai\.com/oauth/authorize\S+)")

MAIL_API_BASE = "https://b.171mail.com/api/v1"
MAIL_DECODE_API = "https://mail.claude-code-manager.com/api/v1/message"
MAIL_POLL_TIMEOUT = 120
MAIL_POLL_INTERVAL = 3

WEBMAIL_PROVIDERS = {
    "163.com": "mailcatcher",
    "mail.com": "mailcom",
    "onet.pl": "onet",
    "gazeta.pl": "gazeta",
}
MAILCATCHER_PROVIDERS = {"mailcatcher", "mailcom", "onet", "gazeta"}
MAIL_PROVIDERS = {"171mail", *MAILCATCHER_PROVIDERS}

EMAIL_SELECTOR = 'input[type="email"], input[name="email"]'
PASSWORD_SELECTOR = 'input[type="password"]'
OTP_SELECTOR = 'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[name="code"]'
OTP_ERROR_SELECTOR = (
    '[role="alert"], [aria-live="assertive"], [data-error-code], '
    '[data-testid*="error"], [class*="error"]'
)
OTP_ERROR_RE = re.compile(
    r"(?:\b(?:invalid|incorrect|wrong|expired)\b.*\bcode\b|"
    r"\bcode\b.*\b(?:invalid|incorrect|wrong|expired)\b|"
    r"\bcode\b.*\bnot valid\b|"
    r"\bcode\b.*(?:does not|doesn't|did not) match|"
    r"could(?: not|n't) verify.*\bcode\b|"
    r"too many (?:verification )?attempts)",
    re.I,
)
CONTINUE_BUTTON_TEXTS = (
    "Continue", "Verify", "Next", "Log in", "Sign in",
    "Authorize", "Allow", "Approve", "Confirm",
)


def _log_codex_login_output(output: str) -> None:
    """Log CLI output without exposing OAuth state or PKCE parameters."""

    safe_output = AUTHORIZE_URL_RE.sub("<OAuth authorize URL redacted>", output.strip())
    logger.info("codex login: %s", safe_output)


# --- OTP polling (171mail API) ---


def detect_mail_provider(email: str) -> str:
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return WEBMAIL_PROVIDERS.get(domain, "171mail")


def _mail_timestamp(data: dict) -> float | None:
    """Return the message timestamp across 171mail/MailCatcher schemas."""
    raw = data.get("date") or data.get("Date")
    if raw:
        value = str(raw).strip()
        try:
            return datetime.datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            try:
                return parsedate_to_datetime(value).timestamp()
            except (TypeError, ValueError):
                pass

    subject = str(data.get("subject") or "")
    match = re.search(r"\|\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", subject)
    if match:
        return time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
    return None


async def poll_verification_code(token: str, after_ts: float, timeout_s: int = MAIL_POLL_TIMEOUT,
                                 email: str = "", provider: str | None = None) -> str:
    """Poll the matching mailbox provider for an OpenAI 6-digit code."""
    deadline = time.time() + timeout_s
    seen: set[tuple[str, str, str]] = set()
    provider = provider or detect_mail_provider(email)
    if provider not in MAIL_PROVIDERS:
        raise ValueError(f"Unsupported mailbox provider: {provider}")
    uses_mailcatcher = provider in MAILCATCHER_PROVIDERS

    # The synchronous MailCatcher endpoint may wait up to 90 seconds for its
    # worker.  Cutting the request off at 45 seconds creates duplicate jobs.
    request_timeout = 120.0 if uses_mailcatcher else 15.0
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        while time.time() < deadline:
            try:
                url = MAIL_DECODE_API if uses_mailcatcher else f"{MAIL_API_BASE}/message"
                resp = await client.get(url, params={"token": token, "type": "gpt"})
                status_code = getattr(resp, "status_code", 200)
                if status_code in {401, 403}:
                    raise RuntimeError("Mailbox API rejected the query token")
                if status_code >= 400:
                    resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            if not isinstance(payload, dict):
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            # MailCatcher has a documented top-level job/result code.  Keep the
            # pre-existing 171mail behavior, whose response need not expose it.
            response_code = payload.get("code")
            if uses_mailcatcher:
                if response_code == 202:
                    await asyncio.sleep(MAIL_POLL_INTERVAL)
                    continue
                if response_code != 200:
                    message = payload.get("message") or payload.get("error") or "unknown error"
                    raise RuntimeError(f"Mailbox API rejected the query token: {message}")

            data = payload.get("data") or {}
            if not isinstance(data, dict):
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue
            subject = data.get("subject") or ""
            code = data.get("code") or ""
            body = data.get("body") or ""
            date_str = data.get("date") or data.get("Date") or ""

            if not (subject or code or body):
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            key = (subject, str(date_str), str(code))
            if key in seen:
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            # MailCatcher's contract returns lowercase ``date``.  It may also
            # encode the timestamp in the subject for older deployments.  Do
            # not accept an undated MailCatcher response: the first poll often
            # still exposes the previous login's OTP.
            mail_ts = _mail_timestamp(data)
            # MailCatcher timestamps may have only whole-second precision.
            # Its result must belong to this request; retain 171mail's legacy
            # grace window to avoid changing that provider's established flow.
            freshness_cutoff = int(after_ts) if uses_mailcatcher else after_ts - 120
            if mail_ts is not None and mail_ts < freshness_cutoff:
                seen.add(key)
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue
            if uses_mailcatcher and mail_ts is None:
                seen.add(key)
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            # Extract 6-digit OTP
            combined = f"{subject} {code} {body}"
            m = re.search(r"\b(\d{6})\b", combined)
            if m:
                logger.info("Got verification code from %s", provider)
                return m.group(1)

            seen.add(key)
            await asyncio.sleep(MAIL_POLL_INTERVAL)

    raise RuntimeError(f"No fresh OpenAI verification code within {timeout_s}s")


# --- Browser state machine ---

async def _click_continue(page, logs: list[str]) -> bool:
    for text in CONTINUE_BUTTON_TEXTS:
        el = await page.query_selector(f'button:has-text("{text}")')
        if el and await el.is_enabled():
            await el.click()
            logs.append(f"Clicked '{text}'")
            return True
    el = await page.query_selector('button[type="submit"]')
    if el and await el.is_enabled():
        await el.click()
        logs.append("Clicked submit")
        return True
    return False


async def _first_visible(page, selectors: str):
    locator = page.locator(selectors)
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def _switch_to_email_code(page, logs: list[str]) -> bool:
    """Switch OpenAI password screen to passwordless email-code login."""
    pattern = re.compile(
        r"continue with (?:an? )?(?:(?:email|one[- ]time|login) )?code|"
        r"use (?:an? )?(?:(?:email|one[- ]time|login) )?code|"
        r"email me (?:an? )?(?:login )?code|"
        r"log in with (?:an? )?(?:one[- ]time )?code|"
        r"sign in with (?:an? )?(?:one[- ]time )?code|"
        r"try another (?:way|method)",
        re.I,
    )
    candidates = page.locator("button, a, [role=button]")
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if not await candidate.is_visible():
            continue
        text = " ".join(filter(None, [
            await candidate.inner_text(), await candidate.get_attribute("aria-label"),
        ]))
        if pattern.search(text):
            await candidate.click()
            for _ in range(10):
                await page.wait_for_timeout(500)
                otp_field = await _first_visible(page, OTP_SELECTOR)
                password_field = await _first_visible(page, PASSWORD_SELECTOR)
                if otp_field or not password_field:
                    logs.append(f"Switched to email-code login via '{text.strip()[:60]}'")
                    return True
            logs.append(f"Email-code action did not change the login page: '{text.strip()[:60]}'")
            return False
    return False


async def _visible_action_labels(page) -> list[str]:
    labels: list[str] = []
    candidates = page.locator("button, a, [role=button]")
    for index in range(min(await candidates.count(), 40)):
        candidate = candidates.nth(index)
        if not await candidate.is_visible():
            continue
        text = " ".join(filter(None, [
            await candidate.inner_text(), await candidate.get_attribute("aria-label"),
        ])).strip()
        if text:
            labels.append(text[:100])
    return labels


async def _visible_otp_error(page) -> str | None:
    """Return an explicit visible OTP rejection, ignoring generic page text."""
    candidates = page.locator(OTP_ERROR_SELECTOR)
    for index in range(min(await candidates.count(), 20)):
        candidate = candidates.nth(index)
        if not await candidate.is_visible():
            continue
        text = (await candidate.inner_text()).strip()
        if text and OTP_ERROR_RE.search(text):
            return text[:200]
    return None


def _emit_login_event(event: dict) -> None:
    """Emit one machine-readable event without logging credentials or OTPs."""
    print(f"{LOGIN_EVENT_PREFIX}{json.dumps(event, separators=(',', ':'))}", flush=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, data: dict) -> None:
    """Atomically persist JSON with owner-only permissions from first byte."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class _ManualOtpReader:
    """Read login initialization and human OTPs over one stdin pipe."""

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._transport = None

    async def _ensure_reader(self) -> asyncio.StreamReader:
        if self._reader is not None:
            return self._reader
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        self._reader = reader
        self._transport = transport
        return reader

    async def read_credentials(
        self,
        *,
        attempt_id: str,
        timeout_s: int = INPUT_INIT_TIMEOUT,
    ) -> tuple[str, str]:
        """Read the first, in-memory-only login initialization message."""
        reader = await self._ensure_reader()
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Timed out waiting for login credentials") from exc
        if not raw:
            raise RuntimeError("Login input channel closed before credentials arrived")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Invalid login credentials message") from exc
        if (
            payload.get("type") != "credentials"
            or payload.get("attempt_id") != attempt_id
        ):
            raise RuntimeError("Invalid login credentials message")
        token = payload.get("token", "")
        password = payload.get("password", "")
        if not isinstance(token, str) or not isinstance(password, str):
            raise RuntimeError("Invalid login credentials message")
        return token, password

    async def read_code(
        self,
        *,
        attempt_id: str,
        timeout_s: int,
        logs: list[str],
    ) -> str:
        reader = await self._ensure_reader()
        challenge_id = uuid.uuid4().hex
        expires_at = int(time.time() + timeout_s)
        _emit_login_event({
            "type": "otp_required",
            "attempt_id": attempt_id,
            "challenge_id": challenge_id,
            "expires_at": expires_at,
        })
        logs.append("Waiting for a user-supplied email verification code")

        while True:
            remaining = expires_at - time.time()
            if remaining <= 0:
                _emit_login_event({
                    "type": "otp_expired",
                    "attempt_id": attempt_id,
                    "challenge_id": challenge_id,
                })
                raise RuntimeError("Timed out waiting for a user-supplied verification code")
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                _emit_login_event({
                    "type": "otp_expired",
                    "attempt_id": attempt_id,
                    "challenge_id": challenge_id,
                })
                raise RuntimeError(
                    "Timed out waiting for a user-supplied verification code"
                ) from exc
            if not raw:
                raise RuntimeError("Verification-code input channel closed")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if payload.get("challenge_id") != challenge_id:
                continue
            code = str(payload.get("code") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            _emit_login_event({
                "type": "otp_received",
                "attempt_id": attempt_id,
                "challenge_id": challenge_id,
            })
            return code

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


async def _run_state_machine(
    page, email: str, password: str, token_171: str,
    timeout: int, auth_path: Path, logs: list[str], mail_provider: str | None = None,
    attempt_id: str = "", manual_otp_reader=None,
) -> None:
    otp_poll_start = time.time()
    deadline = time.time() + timeout
    otp_submitted = False
    otp_attempts = 0
    owns_manual_reader = manual_otp_reader is None
    manual_otp_reader = manual_otp_reader or _ManualOtpReader()

    try:
        while time.time() < deadline:
            await page.wait_for_timeout(STATE_STEP_PAUSE_MS)

            if auth_path.exists():
                logs.append("auth.json appeared — browser flow complete")
                return

            email_field = await _first_visible(page, EMAIL_SELECTOR)
            if email_field and not await email_field.input_value():
                await email_field.fill(email)
                logs.append("Email filled")
                await _click_continue(page, logs)
                continue

            password_field = await _first_visible(page, PASSWORD_SELECTOR)
            if password_field and not await password_field.input_value():
                if not password:
                    if await _switch_to_email_code(page, logs):
                        await page.wait_for_timeout(1500)
                        continue
                    labels = await _visible_action_labels(page)
                    logs.append(f"Password page actions: {labels}")
                    raise RuntimeError(
                        "OpenAI login shows a password field and no email-code option; "
                        "provide the OpenAI password"
                    )
                await password_field.fill(password)
                logs.append("Password filled")
                await _click_continue(page, logs)
                continue

            otp_field = await _first_visible(page, OTP_SELECTOR)
            if otp_field:
                if otp_submitted:
                    # OpenAI can leave the OTP input visible while the callback and
                    # auth.json write complete. Presence alone is never rejection.
                    otp_error = await _visible_otp_error(page)
                    if not otp_error:
                        continue
                    logs.append(f"OpenAI rejected the verification code: {otp_error}")
                    try:
                        await otp_field.fill("")
                    except Exception:
                        pass
                    otp_submitted = False

                if otp_attempts >= MAX_OTP_ATTEMPTS:
                    raise RuntimeError("OpenAI verification code was rejected too many times")

                code = ""
                if token_171:
                    provider = mail_provider or detect_mail_provider(email)
                    logs.append(f"OTP field present, polling {provider}...")
                    try:
                        code = await poll_verification_code(
                            token_171,
                            after_ts=otp_poll_start,
                            email=email,
                            provider=provider,
                        )
                    except Exception as exc:
                        logs.append(
                            f"Mailbox OTP lookup unavailable ({type(exc).__name__}); "
                            "falling back to user input"
                        )

                if not code:
                    wait_started = time.time()
                    code = await manual_otp_reader.read_code(
                        attempt_id=attempt_id,
                        timeout_s=MANUAL_OTP_TIMEOUT,
                        logs=logs,
                    )
                    # Human time should not consume the browser automation budget.
                    deadline += time.time() - wait_started

                await otp_field.fill(code)
                otp_attempts += 1
                otp_submitted = True
                logs.append(f"OTP entered (len={len(code)})")
                await _click_continue(page, logs)
                continue

            # No fillable field — try clicking Continue (consent page)
            await _click_continue(page, logs)

        labels = await _visible_action_labels(page)
        logs.append(f"Timed-out page actions: {labels}")
        raise RuntimeError(f"Login flow did not complete within {timeout}s (url={str(page.url)[:80]})")
    finally:
        if owns_manual_reader:
            manual_otp_reader.close()


# --- Main login flow ---

async def codex_login(
    email: str,
    token_171: str,
    codex_home: str,
    password: str = "",
    timeout: int = DEFAULT_LOGIN_TIMEOUT,
    mail_provider: str | None = None,
    attempt_id: str = "",
    manual_otp_reader=None,
) -> dict:
    """Run the full automated Codex login flow. Returns result dict."""
    t0 = time.time()
    logs: list[str] = []
    codex_home_path = Path(codex_home).expanduser()
    auth_path = codex_home_path / "auth.json"
    attempt_id = attempt_id or uuid.uuid4().hex

    # Do not touch CODEX_HOME or its credentials until all external
    # prerequisites have passed their read-only checks.
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return {"ok": False, "error": "codex CLI not found", "logs": logs}

    if not os.environ.get("DISPLAY"):
        return {"ok": False, "error": "DISPLAY not set — need Xvfb", "logs": logs}

    codex_home_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(codex_home_path, 0o700)
    codex_home = str(codex_home_path)
    logs.append(f"Starting login for {email} (CODEX_HOME={codex_home})")

    had_auth = auth_path.exists()
    backup_path: Path | None = None
    proc: asyncio.subprocess.Process | None = None
    login_succeeded = False
    try:
        # Atomically move the previous credential aside.  A unique path avoids
        # overwriting a recoverable backup left by an interrupted older run.
        if had_auth:
            candidate = codex_home_path / f".auth.json.login-backup-{uuid.uuid4().hex}"
            os.replace(auth_path, candidate)
            backup_path = candidate
            os.chmod(backup_path, 0o600)

        # 1. Start `codex login`
        env = {**os.environ, "CODEX_HOME": codex_home, "NO_COLOR": "1", "TERM": "dumb"}
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "login",
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # 2. Parse authorize URL
        auth_url = None
        deadline = time.time() + AUTH_URL_TIMEOUT
        assert proc.stdout is not None
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue
            if not raw:
                if proc.returncode is not None:
                    break
                await asyncio.sleep(0.2)
                continue
            clean = ANSI_RE.sub("", raw.decode(errors="replace"))
            _log_codex_login_output(clean)
            m = AUTHORIZE_URL_RE.search(clean)
            if m:
                auth_url = m.group(1)
                break

        if not auth_url:
            return {"ok": False, "error": "codex login did not print authorize URL", "logs": logs}

        logs.append(f"Got authorize URL ({len(auth_url)} chars)")

        # 3. Drive browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"ok": False, "error": "playwright not installed", "logs": logs}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                page = await context.new_page()
                logs.append("Navigating to authorize URL")
                await page.goto(auth_url, timeout=45000, wait_until="domcontentloaded")
                await _run_state_machine(
                    page, email, password, token_171, timeout, auth_path, logs, mail_provider,
                    attempt_id, manual_otp_reader,
                )
            finally:
                await browser.close()

        # 4. Wait for auth.json
        wait_deadline = time.time() + AUTH_JSON_WAIT
        while time.time() < wait_deadline:
            if auth_path.exists():
                break
            await asyncio.sleep(1)

        if not auth_path.exists():
            return {"ok": False, "error": "codex did not write auth.json", "logs": logs}

        logs.append("auth.json written by codex")

        # 5. Smoke test
        logs.append("Running smoke test...")
        smoke_ok = await _smoke_test(codex_bin, codex_home, logs)
        if not smoke_ok:
            logs.append("WARNING: smoke test failed, but auth.json exists")

        login_succeeded = True

    finally:
        cleanup_succeeded = False
        try:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            cleanup_succeeded = True
        finally:
            if login_succeeded and cleanup_succeeded:
                if backup_path is not None:
                    try:
                        backup_path.unlink(missing_ok=True)
                    except BaseException:
                        if backup_path.exists():
                            os.replace(backup_path, auth_path)
                        raise
            elif backup_path is not None and backup_path.exists():
                # os.replace atomically overwrites any partial/new auth.json.
                os.replace(backup_path, auth_path)
            elif not had_auth:
                # Restore the original state (no credentials) after a failed
                # first login, including failures during process creation.
                auth_path.unlink(missing_ok=True)

    elapsed = time.time() - t0
    logs.append(f"Login complete in {elapsed:.1f}s")
    return {"ok": True, "elapsed": elapsed, "logs": logs}


async def _smoke_test(codex_bin: str, codex_home: str, logs: list[str]) -> bool:
    env = {**os.environ, "CODEX_HOME": codex_home, "NO_COLOR": "1", "TERM": "dumb"}
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "exec", "echo hello",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        logs.append(f"Smoke test rc={proc.returncode}")
        return proc.returncode == 0
    except asyncio.TimeoutError:
        proc.kill()
        logs.append("Smoke test timed out")
        return False
    except Exception as e:
        logs.append(f"Smoke test error: {e}")
        return False


# --- Pool integration ---

def add_to_codex_pool(
    account_id: str,
    email: str,
    codex_home: str,
    *,
    pool_path: str | os.PathLike[str] | None = None,
):
    """Add an account to the configured Codex pool JSON."""
    pool_path = Path(pool_path) if pool_path else Path.home() / ".codex-pool" / "accounts.json"
    pool_path.parent.mkdir(parents=True, exist_ok=True)

    if pool_path.exists():
        data = json.loads(pool_path.read_text())
    else:
        data = {"accounts": []}

    accounts = data.get("accounts", [])
    # Update if exists, else append
    activated_at = time.time()
    existing = next((a for a in accounts if a["id"] == account_id), None)
    if existing:
        existing["email"] = email
        existing["codex_home"] = codex_home
        existing["enabled"] = True
        existing["quota_valid_after"] = activated_at
        existing.pop("retired", None)
        existing.pop("cleanup_pending", None)
        existing.pop("login_recovery_failed", None)
    else:
        accounts.append({
            "id": account_id,
            "codex_home": codex_home,
            "email": email,
            "enabled": True,
            "quota_valid_after": activated_at,
        })
    data["accounts"] = accounts
    _write_private_json(pool_path, data)
    logger.info("Added %s to codex pool at %s", account_id, pool_path)


def _load_json_object(path: Path, *, default: dict) -> tuple[dict, bool]:
    if not path.exists():
        return json.loads(json.dumps(default)), False
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data, True


def _persist_new_pool_login(
    *,
    account_id: str,
    email: str,
    codex_home: str,
    token: str,
    password: str,
    provider: str,
    pool_path: Path,
    tokens_path: Path,
) -> None:
    """Commit saved credentials and pool registration with rollback."""

    original_tokens, tokens_existed = _load_json_object(tokens_path, default={})
    pool_data, _pool_existed = _load_json_object(
        pool_path, default={"accounts": []},
    )
    accounts = pool_data.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError(f"Expected an accounts list in {pool_path}")

    updated_tokens = json.loads(json.dumps(original_tokens))
    updated_tokens[email] = {
        "token": token,
        "provider": provider,
        "password": password,
    }
    updated_pool = json.loads(json.dumps(pool_data))
    updated_accounts = updated_pool["accounts"]
    activated_at = time.time()
    existing = next(
        (
            account for account in updated_accounts
            if isinstance(account, dict) and account.get("id") == account_id
        ),
        None,
    )
    if existing is None:
        updated_accounts.append({
            "id": account_id,
            "codex_home": codex_home,
            "email": email,
            "enabled": True,
            "quota_valid_after": activated_at,
        })
    else:
        existing.update({
            "codex_home": codex_home,
            "email": email,
            "enabled": True,
            "quota_valid_after": activated_at,
        })
        existing.pop("retired", None)
        existing.pop("cleanup_pending", None)
        existing.pop("login_recovery_failed", None)

    _write_private_json(tokens_path, updated_tokens)
    try:
        _write_private_json(pool_path, updated_pool)
    except BaseException:
        if tokens_existed:
            _write_private_json(tokens_path, original_tokens)
        else:
            tokens_path.unlink(missing_ok=True)
        raise


# --- CLI entry ---

async def main():
    parser = argparse.ArgumentParser(description="Automated Codex login via Playwright + mailbox OTP")
    parser.add_argument("--email", required=True, help="OpenAI account email")
    parser.add_argument(
        "--token",
        default="",
        help="Optional 171mail/MailCatcher query token used only if OpenAI requests an email OTP",
    )
    parser.add_argument("--codex-home", help="CODEX_HOME directory (default: ~/.codex or ~/.codex-<account-id>)")
    parser.add_argument("--password", default="", help="Account password (empty for passwordless)")
    parser.add_argument("--mail-provider", choices=sorted(MAIL_PROVIDERS), default=None,
                        help="OTP mailbox provider (default: detect from email suffix)")
    parser.add_argument("--add-to-pool", metavar="ACCOUNT_ID", help="Add to codex pool after login")
    parser.add_argument("--save-token", action="store_true", help="Save login credentials for future use")
    parser.add_argument("--attempt-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pool-config", default="", help=argparse.SUPPRESS)
    parser.add_argument("--credential-store", default="", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_LOGIN_TIMEOUT)
    args = parser.parse_args()

    input_reader = None
    if args.credentials_stdin:
        if not args.attempt_id:
            parser.error("--credentials-stdin requires --attempt-id")
        input_reader = _ManualOtpReader()
        try:
            args.token, args.password = await input_reader.read_credentials(
                attempt_id=args.attempt_id,
            )
        except BaseException:
            input_reader.close()
            raise
    elif not args.token and not args.password:
        parser.error("at least one of --password or --token is required")

    codex_home = args.codex_home
    if not codex_home:
        if args.add_to_pool:
            if args.add_to_pool == "codex-1":
                codex_home = str(Path.home() / ".codex")
            else:
                codex_home = str(Path.home() / f".codex-{args.add_to_pool}")
        else:
            codex_home = str(Path.home() / ".codex")

    try:
        had_auth_before_login = (Path(codex_home).expanduser() / "auth.json").exists()
        result = await codex_login(
            email=args.email,
            token_171=args.token,
            codex_home=codex_home,
            password=args.password,
            timeout=args.timeout,
            mail_provider=args.mail_provider,
            attempt_id=args.attempt_id,
            manual_otp_reader=input_reader,
        )

        for line in result.get("logs", []):
            print(f"  {line}")

        if result["ok"]:
            print(f"\n✓ Login successful ({result.get('elapsed', 0):.1f}s)")
            tokens_path = (
                Path(args.credential_store)
                if args.credential_store
                else Path.home() / ".codex-pool" / "email_tokens.json"
            )
            pool_path = (
                Path(args.pool_config)
                if args.pool_config
                else Path.home() / ".codex-pool" / "accounts.json"
            )
            try:
                # API add uses both flags. Commit the two files as one logical
                # registration so a pool write failure cannot strand a saved
                # password/token for an account that the pool cannot address.
                if args.save_token and args.add_to_pool:
                    _persist_new_pool_login(
                        account_id=args.add_to_pool,
                        email=args.email,
                        codex_home=codex_home,
                        token=args.token,
                        password=args.password,
                        provider=(
                            args.mail_provider or detect_mail_provider(args.email)
                        ),
                        pool_path=pool_path,
                        tokens_path=tokens_path,
                    )
                else:
                    if args.save_token:
                        tokens, _ = _load_json_object(tokens_path, default={})
                        tokens[args.email] = {
                            "token": args.token,
                            "provider": (
                                args.mail_provider or detect_mail_provider(args.email)
                            ),
                            "password": args.password,
                        }
                        _write_private_json(tokens_path, tokens)
                    if args.add_to_pool:
                        add_to_codex_pool(
                            args.add_to_pool,
                            args.email,
                            codex_home,
                            pool_path=pool_path,
                        )
            except BaseException:
                if args.add_to_pool and not had_auth_before_login:
                    # The API allocator only gives a new account a home with no
                    # auth. Avoid stranding usable OAuth credentials if its
                    # registration transaction cannot commit.
                    (Path(codex_home).expanduser() / "auth.json").unlink(
                        missing_ok=True
                    )
                raise
            if args.add_to_pool:
                print(f"  Added to pool as '{args.add_to_pool}'")
            sys.exit(0)
        else:
            print(f"\n✗ Login failed: {result.get('error')}")
            sys.exit(1)
    finally:
        if input_reader is not None:
            input_reader.close()


if __name__ == "__main__":
    asyncio.run(main())
