"""Codex (OpenAI) automated login — drives `codex login` OAuth flow via Playwright.

Flow:
1. Start `codex login` → it listens on 127.0.0.1:1455 and prints an OAuth authorize URL
2. Parse the authorize URL from stdout (strip ANSI codes)
3. Drive headful real-Chrome Playwright browser through OpenAI login pages:
   - Email field → fill email → Continue
   - Password field → fill password (if present; passwordless accounts skip)
   - OTP field → poll 171mail for 6-digit code → fill → Continue
   - Consent/Authorize button → click
4. Codex captures the callback, exchanges token, writes CODEX_HOME/auth.json
5. Smoke-test with `codex exec`

Prerequisites:
- Xvfb running on :99 (DISPLAY set)
- google-chrome-stable installed
- playwright + playwright-stealth installed
- codex CLI installed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Constants ---

DEFAULT_LOGIN_TIMEOUT = 300
AUTH_URL_TIMEOUT = 30
AUTH_JSON_WAIT = 30
STATE_STEP_PAUSE_MS = 2500

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
AUTHORIZE_URL_RE = re.compile(r"(https://auth\.openai\.com/oauth/authorize\S+)")

MAIL_API_BASE = "https://b.171mail.com/api/v1"
MAIL_POLL_TIMEOUT = 120
MAIL_POLL_INTERVAL = 3

EMAIL_SELECTOR = 'input[type="email"], input[name="email"]'
PASSWORD_SELECTOR = 'input[type="password"]'
OTP_SELECTOR = 'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[name="code"]'
CONTINUE_BUTTON_TEXTS = (
    "Continue", "Verify", "Next", "Log in", "Sign in",
    "Authorize", "Allow", "Approve", "Confirm",
)


# --- 171mail OTP polling ---

async def poll_verification_code(token: str, after_ts: float, timeout_s: int = MAIL_POLL_TIMEOUT) -> str:
    """Poll 171mail for OpenAI 6-digit verification code."""
    import datetime

    deadline = time.time() + timeout_s
    seen: set[tuple[str, str]] = set()

    async with httpx.AsyncClient(timeout=15.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(
                    f"{MAIL_API_BASE}/message",
                    params={"token": token, "type": "gpt"},
                )
                payload = resp.json()
            except Exception:
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            data = payload.get("data") or {}
            subject = data.get("subject") or ""
            code = data.get("code") or ""
            body = data.get("body") or ""
            date_str = data.get("Date") or ""

            if not (subject or code or body):
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            key = (subject, date_str)
            if key in seen:
                await asyncio.sleep(MAIL_POLL_INTERVAL)
                continue

            # Check freshness
            if date_str:
                try:
                    mail_ts = datetime.datetime.fromisoformat(date_str).timestamp()
                    if mail_ts < after_ts - 120:
                        seen.add(key)
                        await asyncio.sleep(MAIL_POLL_INTERVAL)
                        continue
                except ValueError:
                    pass

            # Extract 6-digit OTP
            combined = f"{subject} {code} {body}"
            m = re.search(r"\b(\d{6})\b", combined)
            if m:
                logger.info("Got verification code from 171mail")
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


async def _run_state_machine(
    page, email: str, password: str, token_171: str,
    timeout: int, auth_path: Path, logs: list[str],
) -> None:
    otp_poll_start = time.time()
    deadline = time.time() + timeout
    otp_done = False

    while time.time() < deadline:
        await page.wait_for_timeout(STATE_STEP_PAUSE_MS)

        if auth_path.exists():
            logs.append("auth.json appeared — browser flow complete")
            return

        email_field = await page.query_selector(EMAIL_SELECTOR)
        if email_field and not await email_field.input_value():
            await email_field.fill(email)
            logs.append("Email filled")
            await _click_continue(page, logs)
            continue

        password_field = await page.query_selector(PASSWORD_SELECTOR)
        if password_field and not await password_field.input_value():
            if not password:
                raise RuntimeError("Login page shows password field but no password configured")
            await password_field.fill(password)
            logs.append("Password filled")
            await _click_continue(page, logs)
            continue

        otp_field = await page.query_selector(OTP_SELECTOR)
        if otp_field and not otp_done:
            logs.append("OTP field present, polling 171mail...")
            code = await poll_verification_code(token_171, after_ts=otp_poll_start)
            await otp_field.fill(code)
            otp_done = True
            logs.append(f"OTP entered (len={len(code)})")
            await _click_continue(page, logs)
            continue

        # No fillable field — try clicking Continue (consent page)
        await _click_continue(page, logs)

    raise RuntimeError(f"Login flow did not complete within {timeout}s (url={str(page.url)[:80]})")


# --- Main login flow ---

async def codex_login(
    email: str,
    token_171: str,
    codex_home: str,
    password: str = "",
    timeout: int = DEFAULT_LOGIN_TIMEOUT,
) -> dict:
    """Run the full automated Codex login flow. Returns result dict."""
    t0 = time.time()
    logs: list[str] = []
    auth_path = Path(codex_home) / "auth.json"

    Path(codex_home).mkdir(parents=True, exist_ok=True)

    # Clear stale auth.json so we can detect when codex writes a new one
    if auth_path.exists():
        auth_path.unlink()

    codex_bin = shutil.which("codex")
    if not codex_bin:
        return {"ok": False, "error": "codex CLI not found", "logs": logs}

    if not os.environ.get("DISPLAY"):
        return {"ok": False, "error": "DISPLAY not set — need Xvfb", "logs": logs}

    logs.append(f"Starting login for {email} (CODEX_HOME={codex_home})")

    # 1. Start `codex login`
    env = {**os.environ, "CODEX_HOME": codex_home, "NO_COLOR": "1", "TERM": "dumb"}
    proc = await asyncio.create_subprocess_exec(
        codex_bin, "login",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
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
            logger.info("codex login: %s", clean.strip())
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
                await _run_state_machine(page, email, password, token_171, timeout, auth_path, logs)
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

    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

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

def add_to_codex_pool(account_id: str, email: str, codex_home: str):
    """Add account to ~/.codex-pool/accounts.json."""
    pool_path = Path.home() / ".codex-pool" / "accounts.json"
    pool_path.parent.mkdir(parents=True, exist_ok=True)

    if pool_path.exists():
        data = json.loads(pool_path.read_text())
    else:
        data = {"accounts": []}

    accounts = data.get("accounts", [])
    # Update if exists, else append
    existing = next((a for a in accounts if a["id"] == account_id), None)
    if existing:
        existing["email"] = email
        existing["codex_home"] = codex_home
        existing["enabled"] = True
    else:
        accounts.append({
            "id": account_id,
            "codex_home": codex_home,
            "email": email,
            "enabled": True,
        })
    data["accounts"] = accounts
    pool_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("Added %s to codex pool at %s", account_id, pool_path)


# --- CLI entry ---

async def main():
    parser = argparse.ArgumentParser(description="Automated Codex login via Playwright + 171mail")
    parser.add_argument("--email", required=True, help="OpenAI account email")
    parser.add_argument("--token", required=True, help="171mail token for receiving OTP")
    parser.add_argument("--codex-home", help="CODEX_HOME directory (default: ~/.codex or ~/.codex-<account-id>)")
    parser.add_argument("--password", default="", help="Account password (empty for passwordless)")
    parser.add_argument("--add-to-pool", metavar="ACCOUNT_ID", help="Add to codex pool after login")
    parser.add_argument("--save-token", action="store_true", help="Save 171mail token for future use")
    parser.add_argument("--timeout", type=int, default=DEFAULT_LOGIN_TIMEOUT)
    args = parser.parse_args()

    codex_home = args.codex_home
    if not codex_home:
        if args.add_to_pool:
            if args.add_to_pool == "codex-1":
                codex_home = str(Path.home() / ".codex")
            else:
                codex_home = str(Path.home() / f".codex-{args.add_to_pool}")
        else:
            codex_home = str(Path.home() / ".codex")

    # Save token if requested
    if args.save_token:
        tokens_path = Path.home() / ".codex-pool" / "email_tokens.json"
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        tokens = {}
        if tokens_path.exists():
            try:
                tokens = json.loads(tokens_path.read_text())
            except Exception:
                pass
        tokens[args.email] = args.token
        tokens_path.write_text(json.dumps(tokens, indent=2))
        os.chmod(tokens_path, 0o600)

    result = await codex_login(
        email=args.email,
        token_171=args.token,
        codex_home=codex_home,
        password=args.password,
        timeout=args.timeout,
    )

    for line in result.get("logs", []):
        print(f"  {line}")

    if result["ok"]:
        print(f"\n✓ Login successful ({result.get('elapsed', 0):.1f}s)")
        if args.add_to_pool:
            add_to_codex_pool(args.add_to_pool, args.email, codex_home)
            print(f"  Added to pool as '{args.add_to_pool}'")
        sys.exit(0)
    else:
        print(f"\n✗ Login failed: {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
