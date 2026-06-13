#!/usr/bin/env python3
"""Automated Claude OAuth login pipeline.

End-to-end: register mail service → configure IMAP → OAuth login → get tokens.
Reads verification codes from mail.com webmail via Selenium (bypasses IMAP issues).

Usage:
    # Login single account
    python3 scripts/claude_oauth_login.py --email EMAIL --password PWD --output /tmp/token.json

    # Login all accounts
    python3 scripts/claude_oauth_login.py --all --output-dir /tmp/claude_tokens

    # Register accounts in mail service + login all
    python3 scripts/claude_oauth_login.py --all --register-mail --output-dir /tmp/claude_tokens

Must run under Xvfb:
    xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" python3 scripts/claude_oauth_login.py --all
"""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
MAIL_SERVICE_URL = "https://b.171mail.com"
MAILCATCHER_URL = "https://mail.claude-code-manager.com"
MAIL_ADMIN_USER = "admin"
# AUDIT-FIX C-1: mail admin password no longer hardcoded; require from env at use.
MAIL_ADMIN_PASS = os.environ.get("MAIL_ADMIN_PASS", "")
def _detect_chrome_version() -> int:
    try:
        out = subprocess.check_output(["google-chrome", "--version"], text=True)
        return int(out.strip().split()[-1].split(".")[0])
    except Exception:
        return 149

CHROME_VERSION = _detect_chrome_version()

# AUDIT-FIX C-1: account credentials no longer hardcoded; load from env/file.
def _load_accounts() -> list[dict]:
    """Load built-in accounts from CLAUDE_SEED_ACCOUNTS_JSON (JSON array string)
    or CLAUDE_SEED_ACCOUNTS_FILE (path to JSON array). Returns [] if neither set;
    callers that need accounts must error at the pipeline entry point."""
    raw = os.environ.get("CLAUDE_SEED_ACCOUNTS_JSON")
    if not raw:
        path = os.environ.get("CLAUDE_SEED_ACCOUNTS_FILE")
        if path:
            try:
                with open(path) as f:
                    raw = f.read()
            except OSError:
                return []
    if not raw:
        return []
    try:
        accts = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return accts if isinstance(accts, list) else []


ACCOUNTS = _load_accounts()

# IMAP server for all mail.com-family domains
MAILCOM_IMAP = "imap.mail.com"
MAILCOM_DOMAINS = {
    "lovecat.com", "berlin.com", "consultant.com", "birdlover.com",
    "chemist.com", "tvstar.com", "songwriter.net", "mail.com",
    "email.com", "usa.com", "post.com", "europe.com", "asia.com",
    "iname.com", "writeme.com", "dr.com", "cheerful.com",
    "techie.com", "myself.com",
}

# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Mail service registration
# ---------------------------------------------------------------------------

def _http_json(url: str, data: dict | None = None, headers: dict | None = None) -> dict:
    """HTTP request via curl (avoids urllib User-Agent blocks)."""
    cmd = ["curl", "-sL", "--max-time", "15"]
    if data is not None:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json",
                "-d", json.dumps(data)]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    resp = subprocess.check_output(cmd).decode()
    return json.loads(resp)


def _mail_admin_token() -> str:
    # AUDIT-FIX C-1: fail clearly if mail admin password not provided via env.
    if not MAIL_ADMIN_PASS:
        raise SystemExit("MAIL_ADMIN_PASS not set (env var required for mail service login)")
    body = _http_json(
        f"{MAIL_SERVICE_URL}/api/admin/login",
        data={"username": MAIL_ADMIN_USER, "password": MAIL_ADMIN_PASS},
    )
    return body["data"]["accessToken"]


def register_mail_accounts(accounts: list[dict]) -> dict[str, str]:
    """Register email accounts in the mail service. Returns {email: mail_token}."""
    token = _mail_admin_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Get existing accounts
    existing = _http_json(
        f"{MAIL_SERVICE_URL}/api/admin/email/list?page=1&pageSize=100&keyword=",
        headers=headers,
    )
    existing_emails = {
        e["address"]: e["token"]
        for e in existing.get("data", {}).get("list", [])
    }

    # Get existing mail servers
    servers = _http_json(
        f"{MAIL_SERVICE_URL}/api/admin/mail-server/list?keyword=",
        headers=headers,
    )
    existing_domains = {s["domain"] for s in servers.get("data", {}).get("list", [])}

    results = {}
    for acct in accounts:
        email = acct["email"]
        domain = email.split("@")[1]

        # Ensure mail server config exists
        if domain not in existing_domains and domain in MAILCOM_DOMAINS:
            try:
                _http_json(
                    f"{MAIL_SERVICE_URL}/api/admin/mail-server/create",
                    data={"domain": domain, "host": MAILCOM_IMAP, "port": 993, "use_ssl": 1, "status": 1},
                    headers=headers,
                )
                existing_domains.add(domain)
                print(f"  [mail] Created server config for {domain}")
            except Exception as e:
                print(f"  [mail] Server config for {domain} failed: {e}")

        # Register email if not exists
        if email in existing_emails:
            results[email] = existing_emails[email]
            print(f"  [mail] {email} already registered (token={results[email][:8]}...)")
        else:
            try:
                result = _http_json(
                    f"{MAIL_SERVICE_URL}/api/admin/email/create",
                    data={"address": email, "password": acct["password"]},
                    headers=headers,
                )
                mail_token = result.get("data", {}).get("token", "")
                results[email] = mail_token
                print(f"  [mail] Registered {email} (token={mail_token[:8]}...)")
            except Exception as e:
                print(f"  [mail] Failed to register {email}: {e}")

    return results


# ---------------------------------------------------------------------------
# Webmail verification code reader
# ---------------------------------------------------------------------------

def _create_driver(clean_profile: bool = False):
    """Create an undetected Chrome driver (requires Xvfb or real display).

    When *clean_profile* is True, a fresh temporary user-data-dir is used so
    no stale cookies/sessions from a previous run can interfere.
    """
    import undetected_chromedriver as uc

    opts = uc.ChromeOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.page_load_strategy = "eager"
    if clean_profile:
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="uc_profile_")
        opts.add_argument(f"--user-data-dir={tmpdir}")
    return uc.Chrome(options=opts, version_main=CHROME_VERSION)


def _login_mailcom(driver, email: str, password: str) -> str | None:
    """Login to mail.com webmail. Returns the mail client iframe URL or None."""
    from selenium.webdriver.common.by import By

    print(f"  [webmail] Logging into mail.com for {email}...")
    try:
        driver.get("https://www.mail.com/")
        time.sleep(3)
    except Exception as e:
        print(f"  [webmail] Login error: {e}")
        return None

    try:
        # Click "Log in" link on mail.com homepage
        driver.execute_script("""
            for (const a of document.querySelectorAll('a')) {
                const t = a.textContent.trim().toLowerCase();
                if (t === 'log in' || t === 'login' || t === 'sign in') {
                    a.click(); return;
                }
            }
        """)
        time.sleep(2)

        email_input = driver.find_element(By.ID, "login-email")
        pwd_input = driver.find_element(By.ID, "login-password")
        email_input.clear()
        email_input.send_keys(email)
        pwd_input.clear()
        pwd_input.send_keys(password)
        time.sleep(0.5)

        btn = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
        btn.click()
        print(f"  [webmail] Submitted credentials, waiting for inbox...")
        time.sleep(12)

        slug = email.split("@")[0]
        driver.save_screenshot(f"/tmp/mailcom_login_{slug}.png")

        # Wait for the mail client iframe to appear (can take 10-30s after login)
        mail_client_url = None
        for wait_i in range(12):
            # Method 1: find iframe elements
            for f in driver.find_elements(By.TAG_NAME, "iframe"):
                src = f.get_attribute("src") or ""
                if "mail/client" in src or "thirdPartyFrame" in f.get_attribute("id") or "":
                    if src and ("mail" in src or "3c" in src):
                        mail_client_url = src
                        break
            if mail_client_url:
                break

            # Method 2: regex on page source for mail client URLs
            page_src = driver.page_source
            for pattern in [
                r'(https://3c[^"\'>\s]+/mail/client[^"\'>\s]*)',
                r'src="(https://[^"]+/mail/client[^"]*)"',
                r'(https://[^"\'>\s]+thirdPartyFrame[^"\'>\s]*)',
            ]:
                m = re.search(pattern, page_src)
                if m:
                    mail_client_url = m.group(1).replace("&amp;", "&")
                    break
            if mail_client_url:
                break

            # Method 3: check if we were redirected directly to the mail client
            if "/mail/client" in driver.current_url:
                mail_client_url = driver.current_url
                break

            if wait_i % 3 == 2:
                print(f"  [webmail] Still waiting for inbox iframe... ({12 + wait_i*3}s)")
            time.sleep(3)

        if mail_client_url:
            print(f"  [webmail] Login successful, mail client URL captured")
            return mail_client_url

        # Last resort: save a diagnostic screenshot and check the page title/URL
        driver.save_screenshot(f"/tmp/mailcom_login_{slug}_failed.png")
        print(f"  [webmail] Login OK ({driver.title}) but no mail iframe found")
        print(f"  [webmail] URL: {driver.current_url[:120]}")
        return None

    except Exception as e:
        print(f"  [webmail] Login error: {e}")
        return None




# ---------------------------------------------------------------------------
# Claude OAuth flow
# ---------------------------------------------------------------------------

def do_oauth_login(
    email: str,
    password: str,
    mail_token: str | None = None,
    output_path: str | None = None,
) -> dict | None:
    """Complete Claude OAuth login using a single browser with two tabs.

    Tab 0: mail.com webmail (for reading verification codes)
    Tab 1: Claude OAuth flow
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    print(f"\n{'='*60}")
    print(f"OAuth login: {email}")
    print(f"{'='*60}")

    verifier, challenge = _generate_pkce()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()

    auth_url = (
        f"https://claude.ai/oauth/authorize?"
        f"code=true&client_id={CLIENT_ID}&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&scope={urllib.parse.quote(SCOPES, safe='')}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
        f"&state={state}&login_hint={urllib.parse.quote(email, safe='')}"
    )

    driver = _create_driver()
    slug = email.split("@")[0]
    webmail_ok = False

    try:
        # ── Phase 1: Login to mail.com (same tab, we'll navigate back later) ──
        mail_client_url = _login_mailcom(driver, email, password)
        webmail_ok = mail_client_url is not None

        # If webmail login failed, the driver session may be corrupted.
        # Tab crashes leave the session alive but the tab unusable, so
        # test with an actual navigation, not just execute_script.
        if not webmail_ok:
            try:
                driver.get("about:blank")
                driver.execute_script("return 1")
            except Exception:
                print("  [webmail] Driver session corrupted after mail.com failure, creating new browser...")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = _create_driver(clean_profile=True)

        # ── Phase 2: Navigate to Claude OAuth ──
        # Warm up: visit claude.ai first to get Cloudflare cookies
        print("  [claude] Warming up browser (visiting claude.ai)...")
        driver.get("https://claude.ai/")
        for i in range(40):
            title = driver.title.lower()
            if "just a moment" not in title and "security" not in title:
                break
            time.sleep(1)
            if i % 10 == 9:
                print(f"  [claude] Cloudflare warmup... ({i+1}s)")
        time.sleep(2)
        driver.save_screenshot(f"/tmp/oauth_{slug}_0_warmup.png")
        print(f"  [claude] Warmup done: {driver.title}")
        # Clear auth state so login_hint forces a fresh sign-in flow.
        # Keep Cloudflare cookies (cf_clearance, __cf_bm) but wipe
        # everything else including localStorage/sessionStorage.
        for cookie in driver.get_cookies():
            name = cookie.get("name", "")
            if name.startswith("cf_") or name == "__cf_bm":
                continue
            driver.delete_cookie(name)
        try:
            driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
        except Exception:
            pass

        print("  [claude] Opening authorize URL...")
        driver.get(auth_url)

        # Wait for Cloudflare (should be faster with cookies from warmup)
        for i in range(30):
            title = driver.title.lower()
            if "just a moment" not in title and "security" not in title:
                break
            time.sleep(1)
            if i % 10 == 9:
                print(f"  [claude] Cloudflare challenge... ({i+1}s)")

        time.sleep(2)
        driver.save_screenshot(f"/tmp/oauth_{slug}_1_after_cf.png")
        print(f"  [claude] Page: {driver.title}")

        if "just a moment" in driver.title.lower():
            print("  [claude] FAILED: Cloudflare blocked")
            return None

        # Handle initial login page — click "Continue with email", enter email, submit
        page_text = driver.find_element(By.TAG_NAME, "body").text

        if "continue with email" in page_text.lower():
            # Use Selenium click (JS click doesn't work reliably here)
            for b in driver.find_elements(By.TAG_NAME, "button"):
                if "continue with email" in b.text.strip().lower():
                    b.click()
                    print("  [claude] Clicked 'Continue with email'")
                    break
            time.sleep(3)

        # Enter email if input field is visible
        try:
            email_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="email"], input[name="email"]'))
            )
            if email_input.is_displayed():
                email_input.clear()
                email_input.send_keys(email)
                time.sleep(0.5)
                for b in driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]'):
                    if b.is_displayed():
                        b.click()
                        print("  [claude] Submitted email")
                        break
        except Exception:
            pass  # login_hint may have auto-submitted

        time.sleep(3)
        driver.save_screenshot(f"/tmp/oauth_{slug}_2_after_email.png")

        page_text = driver.find_element(By.TAG_NAME, "body").text
        print(f"  [claude] Page after email: {page_text[:200]}")

        # Try password if available
        try:
            pwd_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
            if pwd_input.is_displayed():
                pwd_input.clear()
                pwd_input.send_keys(password)
                driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
                print("  [claude] Submitted password")
                time.sleep(3)
                page_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            pass

        # Handle verification: Claude sends an email with a code or magic link.
        verification_trigger_time = time.time()
        page_lower = page_text.lower()
        needs_verification = (
            "verification" in page_lower
            or "check your email" in page_lower
            or "click the link" in page_lower
            or "enter the" in page_lower
            or "sent to" in page_lower
        )
        # If the page is empty/unexpected but we haven't reached the consent
        # page yet, assume verification is needed (the email was submitted).
        if not needs_verification and "authorize" not in page_lower and "allow" not in page_lower:
            if not page_text.strip() or len(page_text.strip()) < 20:
                print("  [claude] Page text empty/unexpected — assuming verification needed")
                needs_verification = True
        if needs_verification:
            print("  [claude] Verification required — waiting 5s for email delivery, then checking webmail...")
            time.sleep(5)

            # Check if the page wants a verification code (text input) or a magic link
            wants_code = "verification code" in page_text.lower() or "enter.*code" in page_text.lower()

            result = None
            if webmail_ok and mail_client_url:
                result = _read_verification_from_webmail(driver, email, mail_client_url, max_wait=90)

            if not result and mail_token:
                print("  [claude] Webmail unavailable, trying MailCatcher API...")
                # mail.com 域查 MailCatcher，其余查 171mail
                domain = email.split("@")[-1].lower()
                svc = MAILCATCHER_URL if domain in MAILCOM_DOMAINS else MAIL_SERVICE_URL
                result = _read_verification_from_mailcatcher(mail_token, max_wait=90, service_url=svc)

            if not result:
                print("  [claude] FAILED: No verification code or magic link found")
                return None

            if result.startswith("CODE:"):
                code = result[5:]
                print(f"  [claude] Got verification code: {code}")
                # Navigate back to the Claude verification page
                driver.get(auth_url)
                for i in range(30):
                    if "just a moment" not in driver.title.lower():
                        break
                    time.sleep(1)
                time.sleep(3)

                # Re-enter email to get back to verification code page
                page_text = driver.find_element(By.TAG_NAME, "body").text
                if "continue with email" in page_text.lower():
                    for b in driver.find_elements(By.TAG_NAME, "button"):
                        if "continue with email" in b.text.strip().lower():
                            b.click()
                            break
                    time.sleep(2)

                try:
                    email_input = driver.find_element(By.CSS_SELECTOR, 'input[type="email"], input[name="email"]')
                    if email_input.is_displayed():
                        email_input.clear()
                        email_input.send_keys(email)
                        time.sleep(0.5)
                        for b in driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]'):
                            if b.is_displayed():
                                b.click()
                                break
                        time.sleep(3)
                except Exception:
                    pass

                # Enter the verification code
                try:
                    code_input = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="code"], input[placeholder*="code"], input[placeholder*="verification"], input[type="text"]'))
                    )
                    code_input.clear()
                    code_input.send_keys(code)
                    time.sleep(0.5)
                    for b in driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]'):
                        if b.is_displayed():
                            b.click()
                            print("  [claude] Submitted verification code")
                            break
                    time.sleep(5)
                except Exception as e:
                    print(f"  [claude] Code input error: {e}")
                    return None

            else:
                # Magic link — use it to authenticate.
                # driver.get() can crash Chrome on long fragment URLs;
                # fall back to JS navigation and then a fresh page load.
                print(f"  [claude] Navigating to magic link...")
                navigated = False
                for attempt in range(3):
                    try:
                        if attempt == 0:
                            driver.execute_script(f"window.location.href = {json.dumps(result)};")
                        else:
                            driver.get(result)
                        navigated = True
                        break
                    except Exception as nav_err:
                        print(f"  [claude] Magic link navigation attempt {attempt+1} failed: {nav_err}")
                        time.sleep(2)
                if not navigated:
                    print("  [claude] FAILED: Could not navigate to magic link")
                    return None
                for i in range(30):
                    try:
                        if "magic-link" not in driver.current_url:
                            break
                    except Exception:
                        break
                    time.sleep(1)
                    if i % 5 == 4:
                        print(f"  [claude] Waiting for magic link redirect... ({i+1}s)")
                time.sleep(3)

            driver.save_screenshot(f"/tmp/oauth_{slug}_3_after_verify.png")
            print(f"  [claude] After verification: {driver.current_url[:100]}")

        # ── Phase 3: Authorize via API (bypass Arkose challenge on consent page) ──
        # Get org UUID from the authenticated session
        print("  [claude] Getting org UUID...")
        driver.get("https://claude.ai/api/organizations")
        time.sleep(3)
        try:
            org_data = json.loads(driver.find_element(By.TAG_NAME, "body").text)
        except Exception:
            org_data = driver.execute_async_script("""
                var cb = arguments[arguments.length - 1];
                fetch('/api/organizations').then(r => r.json()).then(d => cb(d)).catch(e => cb({error: e.message}));
            """)

        org_uuid = None
        if isinstance(org_data, list) and org_data:
            org_uuid = org_data[0].get("uuid")
        if not org_uuid:
            print(f"  [claude] FAILED: Could not get org UUID: {str(org_data)[:200]}")
            return None
        print(f"  [claude] Org: {org_uuid}")

        # Navigate to auth URL so we're on claude.ai domain (for cookies)
        driver.get(auth_url)
        for i in range(30):
            if "just a moment" not in driver.title.lower():
                break
            time.sleep(1)
        time.sleep(3)

        # POST to authorize endpoint from browser (bypasses Arkose challenge)
        print("  [claude] Submitting authorization via API...")
        auth_resp = driver.execute_async_script("""
            var cb = arguments[arguments.length - 1];
            var orgUuid = arguments[0];
            var body = arguments[1];
            fetch('/v1/oauth/' + orgUuid + '/authorize', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: body,
            }).then(function(r) {
                return r.text().then(function(t) { cb({status: r.status, body: t}); });
            }).catch(function(e) { cb({error: e.message}); });
        """, org_uuid, json.dumps({
            "response_type": "code",
            "client_id": CLIENT_ID,
            "organization_uuid": org_uuid,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }))

        auth_code = None
        if auth_resp and auth_resp.get("status") == 200:
            try:
                resp_data = json.loads(auth_resp["body"])
                redirect_url = resp_data.get("redirect_uri", "")
                params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(redirect_url).query))
                auth_code = params.get("code")
                if auth_code:
                    print(f"  [claude] Got auth code: {auth_code[:20]}...")
            except Exception as e:
                print(f"  [claude] Parse error: {e}")
        else:
            err_msg = auth_resp.get("body", "") if auth_resp else "no response"
            print(f"  [claude] Authorize failed (status={auth_resp.get('status') if auth_resp else 'N/A'}): {err_msg[:200]}")

        if not auth_code:
            print("  [claude] FAILED: No auth code obtained")
            return None

        # Save auth state for retry (auth codes expire in ~10 min)
        auth_state_path = f"/tmp/claude_tokens/auth_state_{slug}.json"
        os.makedirs(os.path.dirname(auth_state_path), exist_ok=True)
        with open(auth_state_path, "w") as f:
            json.dump({
                "email": email,
                "auth_code": auth_code,
                "code_verifier": verifier,
                "timestamp": time.time(),
                "org_uuid": org_uuid,
            }, f, indent=2)
        print(f"  [claude] Auth state saved: {auth_state_path}")

        # Exchange auth code for tokens
        # Try browser fetch first (uses browser's TLS/cookies, avoids IP-level rate limits),
        # then fall back to curl with retries.
        print("  [claude] Exchanging code for tokens...")
        exchange_payload = json.dumps({
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        })

        token_resp = None

        # Method 1: Browser fetch (navigate to console.anthropic.com first for same-origin)
        try:
            driver.get("https://console.anthropic.com/")
            time.sleep(3)
            browser_resp = driver.execute_async_script("""
                var cb = arguments[arguments.length - 1];
                var payload = arguments[0];
                fetch('https://console.anthropic.com/v1/oauth/token', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: payload,
                }).then(function(r) {
                    return r.text().then(function(t) { cb({status: r.status, body: t}); });
                }).catch(function(e) { cb({error: e.message}); });
            """, exchange_payload)
            if browser_resp and not browser_resp.get("error"):
                try:
                    token_resp = json.loads(browser_resp["body"])
                    if "access_token" in token_resp:
                        print(f"  [claude] Token obtained via browser fetch")
                except Exception:
                    pass
            if token_resp and "rate_limit" in str(token_resp).lower():
                print(f"  [claude] Browser fetch also rate limited, trying curl...")
                token_resp = None
        except Exception as e:
            print(f"  [claude] Browser fetch failed: {e}")

        # Method 2: curl with retries
        if not token_resp or "access_token" not in token_resp:
            retry_delays = [5, 15, 30, 60, 120]
            for attempt in range(len(retry_delays) + 1):
                try:
                    token_resp = _http_json(
                        "https://console.anthropic.com/v1/oauth/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": auth_code,
                            "redirect_uri": REDIRECT_URI,
                            "client_id": CLIENT_ID,
                            "code_verifier": verifier,
                        },
                    )
                    if "access_token" in token_resp:
                        break
                    if "rate_limit" in str(token_resp).lower():
                        if attempt < len(retry_delays):
                            wait = retry_delays[attempt]
                            print(f"  [claude] Rate limited, waiting {wait}s (attempt {attempt+1}/{len(retry_delays)+1})...")
                            time.sleep(wait)
                        else:
                            print(f"  [claude] Rate limited after all retries")
                    else:
                        print(f"  [claude] Token exchange error: {str(token_resp)[:200]}")
                        break
                except Exception as e:
                    print(f"  [claude] Token exchange error: {e}")
                    if attempt < len(retry_delays):
                        time.sleep(5)

        if not token_resp or "access_token" not in token_resp:
            print(f"  [claude] Token exchange failed: {token_resp}")
            return None

        creds = {
            "claudeAiOauth": {
                "accessToken": token_resp["access_token"],
                "refreshToken": token_resp.get("refresh_token", ""),
                "expiresAt": int((time.time() + token_resp.get("expires_in", 43200)) * 1000),
                "scopes": SCOPES.split(),
            }
        }

        if output_path:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(creds, f, indent=2)
            os.chmod(output_path, 0o600)

        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(creds["claudeAiOauth"]["expiresAt"] / 1000))
        print(f"  [claude] SUCCESS — expires {exp}")
        if output_path:
            print(f"  [claude] Saved: {output_path}")
        return creds

    finally:
        driver.quit()


def _extract_magic_link(driver) -> str | None:
    """Extract the Claude magic link URL from the current email view (handles iframes)."""
    from selenium.webdriver.common.by import By

    def _find_magic_link_in_context(drv) -> str | None:
        """Find a claude.ai magic link in the current frame context."""
        for link in drv.find_elements(By.TAG_NAME, "a"):
            href = link.get_attribute("href") or ""
            text = link.text.strip().lower()
            if "sign in" in text or "log in" in text or "verify" in text:
                # Extract the actual redirect URL from mail.com's dereferrer
                m = re.search(r"redirectUrl=([^&]+)", href)
                if m:
                    actual_url = urllib.parse.unquote(m.group(1))
                    if "claude.ai" in actual_url:
                        return actual_url
                if "claude.ai" in href:
                    return href
        # Also check page source for direct magic-link URLs
        src = drv.page_source
        m = re.search(r'(https://claude\.ai/magic-link[^"\'<>\s]+)', src)
        if m:
            return m.group(1)
        return None

    # Check main page
    url = _find_magic_link_in_context(driver)
    if url:
        return url

    # Check iframes (email body is typically in a 'mailbody' iframe)
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for idx, iframe in enumerate(iframes):
        try:
            src_attr = iframe.get_attribute("src") or ""
            driver.switch_to.frame(iframe)
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if body_text.strip() and ("sign in" in body_text.lower() or "anthropic" in body_text.lower()):
                url = _find_magic_link_in_context(driver)
                if url:
                    driver.switch_to.default_content()
                    return url
            driver.switch_to.default_content()
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    return None


def _click_latest_anthropic_email(driver, keywords: list[str] | None = None) -> bool:
    """Click the most recent Anthropic email in the mail.com inbox.
    If keywords given, prefer emails containing those words (e.g. ['verification', 'code']).
    Returns True if clicked.
    """
    kw_js = json.dumps(keywords or [])
    clicked = driver.execute_script("""
        var keywords = JSON.parse(arguments[0]);
        function matchesKeywords(text) {
            if (keywords.length === 0) return true;
            var lower = text.toLowerCase();
            return keywords.some(function(k) { return lower.includes(k.toLowerCase()); });
        }

        // Try table rows first (mail.com uses tables for inbox)
        var rows = document.querySelectorAll('tr');
        var bestRow = null;
        for (var r of rows) {
            var t = r.textContent;
            if (t.includes('Anthropic') && t.length < 300) {
                if (matchesKeywords(t)) {
                    r.click();
                    return 'keyword_match';
                }
                if (!bestRow) bestRow = r;
            }
        }

        // Try list/div items
        var items = document.querySelectorAll('li, a, div');
        var bestItem = null;
        for (var el of items) {
            var t = el.textContent;
            if (t.includes('Anthropic') && t.length < 300 && el.offsetHeight > 15 && el.offsetHeight < 80) {
                if (matchesKeywords(t)) {
                    el.click();
                    return 'keyword_match';
                }
                if (!bestItem) bestItem = el;
            }
        }

        // Fall back to first Anthropic email found
        if (bestRow) { bestRow.click(); return 'first_match'; }
        if (bestItem) { bestItem.click(); return 'first_match'; }
        return false;
    """, kw_js)
    return bool(clicked)


def _extract_code_from_frames(driver) -> str | None:
    """Extract a 6-digit verification code from the current page and its iframes."""
    from selenium.webdriver.common.by import By

    def _find_code_in_text(text: str) -> str | None:
        codes = re.findall(r"\b(\d{6})\b", text)
        for c in codes:
            yr = int(c[:4]) if len(c) >= 4 else 0
            if 1900 < yr < 2100:
                continue
            return c
        return None

    # Check main page
    try:
        code = _find_code_in_text(driver.find_element(By.TAG_NAME, "body").text)
        if code:
            return code
    except Exception:
        pass

    # Check iframes (email body)
    for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.frame(iframe)
            iframe_text = driver.find_element(By.TAG_NAME, "body").text
            code = _find_code_in_text(iframe_text)
            if code:
                driver.switch_to.default_content()
                return code
            driver.switch_to.default_content()
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return None


def _read_verification_from_mailcatcher(mail_token: str, max_wait: int = 120, service_url: str | None = None) -> str | None:
    """Fetch verification code or magic link via mail service API.

    service_url: 171mail (https://b.171mail.com) or MailCatcher (https://mail.claude-code-manager.com).
    Both expose the same /api/v1/message?token=&type= endpoint.

    Returns "CODE:123456", a magic-link URL string, or None.
    """
    base = service_url or MAIL_SERVICE_URL
    print(f"  [mailcatcher] Polling {base} for verification (max {max_wait}s)...")
    start = time.time()
    attempt = 0
    while time.time() - start < max_wait:
        attempt += 1
        try:
            url = f"{base}/api/v1/message?token={mail_token}&type=claude"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if data.get("code") == 200:
                payload = data.get("data", {})
                code_or_link = payload.get("code") or ""
                if code_or_link.startswith("http"):
                    print(f"  [mailcatcher] Got magic link: {code_or_link[:80]}...")
                    return code_or_link
                if code_or_link and code_or_link.isdigit():
                    print(f"  [mailcatcher] Got verification code: {code_or_link}")
                    return f"CODE:{code_or_link}"
                body = payload.get("body", "")
                m = re.search(r'(https://claude\.ai/magic-link[^\s"\'<>]+)', body)
                if m:
                    print(f"  [mailcatcher] Got magic link from body: {m.group(1)[:80]}...")
                    return m.group(1)
            elapsed = int(time.time() - start)
            print(f"  [mailcatcher] No verification yet ({elapsed}s, attempt {attempt})")
        except Exception as e:
            print(f"  [mailcatcher] API error: {e}")
        time.sleep(8)
    return None


def _read_verification_from_webmail(driver, email: str, mail_client_url: str, max_wait: int = 120) -> str | None:
    """Navigate to mail.com client, find the latest Anthropic verification code or magic link.

    Returns one of:
      - "CODE:123456" — a 6-digit verification code
      - "https://..." — a magic link URL
      - None — nothing found
    """
    from selenium.webdriver.common.by import By

    print(f"  [webmail] Navigating to mail client for verification...")
    driver.get(mail_client_url)
    time.sleep(6)

    slug = email.split("@")[0]
    start = time.time()
    attempt = 0

    while time.time() - start < max_wait:
        attempt += 1
        try:
            if attempt > 1:
                driver.refresh()
                time.sleep(5)

            body_text = driver.find_element(By.TAG_NAME, "body").text

            if "anthropic" not in body_text.lower() and "claude" not in body_text.lower():
                elapsed = int(time.time() - start)
                print(f"  [webmail] No Anthropic email yet ({elapsed}s)")
                time.sleep(8)
                continue

            # Try to click an email with "verification" or "code" first, then any Anthropic email
            clicked = _click_latest_anthropic_email(driver, keywords=["verification", "code", "verify"])
            if not clicked:
                clicked = _click_latest_anthropic_email(driver)

            if clicked:
                print(f"  [webmail] Opened Anthropic email (attempt {attempt})")
                time.sleep(4)
                driver.save_screenshot(f"/tmp/webmail_{slug}_email_{attempt}.png")

                # Priority 1: Look for 6-digit verification code
                code = _extract_code_from_frames(driver)
                if code:
                    print(f"  [webmail] Found verification code: {code}")
                    return f"CODE:{code}"

                # Priority 2: Look for magic link
                magic_url = _extract_magic_link(driver)
                if magic_url:
                    print(f"  [webmail] Found magic link: {magic_url[:80]}...")
                    return magic_url

                # Log what we found for debugging
                for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
                    try:
                        iframe_id = iframe.get_attribute("id") or ""
                        iframe_src = (iframe.get_attribute("src") or "")[:60]
                        driver.switch_to.frame(iframe)
                        text = driver.find_element(By.TAG_NAME, "body").text
                        if text.strip():
                            print(f"  [webmail] iframe({iframe_id},{iframe_src}): {text[:150]}")
                        driver.switch_to.default_content()
                    except Exception:
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass

                print(f"  [webmail] Email opened but no code or link found, going back")
                driver.back()
                time.sleep(3)
            else:
                print(f"  [webmail] Anthropic text visible but couldn't click email")

        except Exception as e:
            print(f"  [webmail] Error: {e}")

        time.sleep(5)

    print(f"  [webmail] Timeout after {max_wait}s")
    return None



# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    accounts: list[dict],
    output_dir: str,
    register_mail: bool = True,
) -> dict[str, dict]:
    """Run the full login pipeline for all accounts."""
    os.makedirs(output_dir, exist_ok=True)
    results: dict[str, dict] = {}

    # Step 1: Register accounts in mail service
    mail_tokens: dict[str, str] = {}
    if register_mail:
        print("\n[Pipeline] Registering accounts in mail service...")
        mail_tokens = register_mail_accounts(accounts)

    # Step 2: Login to each account sequentially with delay between accounts
    for idx, acct in enumerate(accounts):
        email = acct["email"]
        password = acct["password"]
        out_path = os.path.join(output_dir, f"{email.replace('@','_at_')}.json")
        mail_token = mail_tokens.get(email)

        if idx > 0:
            print(f"\n[Pipeline] Waiting 30s between accounts...")
            time.sleep(30)

        try:
            creds = do_oauth_login(
                email=email,
                password=password,
                mail_token=mail_token,
                output_path=out_path,
            )
            results[email] = {
                "status": "ok" if creds else "failed",
                "path": out_path if creds else None,
                "credentials": creds,
            }
        except Exception as e:
            print(f"  [pipeline] Error for {email}: {e}")
            results[email] = {"status": "error", "error": str(e)}

    # Summary
    print(f"\n{'='*60}")
    print("PIPELINE RESULTS")
    print(f"{'='*60}")
    ok = 0
    for email, r in results.items():
        status = r["status"]
        if status == "ok":
            ok += 1
            print(f"  OK    {email}")
        else:
            err = r.get("error", "unknown")
            print(f"  FAIL  {email}: {err}")
    print(f"\nTotal: {ok}/{len(results)} succeeded")

    return results


# ---------------------------------------------------------------------------
# SaaS import
# ---------------------------------------------------------------------------

def import_tokens_to_saas(token_dir: str, saas_url: str) -> None:
    """Import token JSON files to SaaS DB via admin API."""
    import glob as globmod

    token_files = sorted(globmod.glob(os.path.join(token_dir, "*_at_*.json")))
    if not token_files:
        print("[import] No token files found")
        return

    accounts = []
    for tf in token_files:
        with open(tf) as f:
            creds = json.load(f)
        email = os.path.basename(tf).replace("_at_", "@").replace(".json", "")
        accounts.append({"email": email, "oauth_token": creds})
        print(f"  [import] {email}")

    payload = json.dumps({"accounts": accounts})
    try:
        resp = subprocess.check_output([
            "curl", "-sL", "--max-time", "15",
            "-X", "POST", f"{saas_url}/api/v1/admin/claude-accounts/import",
            "-H", "Content-Type: application/json",
            "-d", payload,
        ]).decode()
        result = json.loads(resp)
        print(f"  [import] Result: {json.dumps(result, indent=2)}")
    except Exception as e:
        print(f"  [import] Error: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Claude OAuth automated login pipeline")
    parser.add_argument("--email", help="Single account email")
    parser.add_argument("--password", help="Single account password")
    parser.add_argument("--mail-token", help="Mail service token (for single account)")
    parser.add_argument("--output", help="Output path for single account")
    parser.add_argument("--all", action="store_true", help="Login all built-in accounts")
    parser.add_argument("--output-dir", default="/tmp/claude_tokens", help="Output dir (default: /tmp/claude_tokens)")
    parser.add_argument("--register-mail", action="store_true", help="Register accounts in mail service first")
    parser.add_argument("--dry-run", action="store_true", help="Only register mail, don't do OAuth")
    parser.add_argument("--exchange-only", action="store_true", help="Only exchange saved auth codes for tokens")
    parser.add_argument("--import-to-saas", help="SaaS API base URL to import tokens (e.g. https://api.example.com)")
    args = parser.parse_args()

    if args.exchange_only:
        # Retry token exchange for all saved auth states
        import glob as globmod
        state_files = sorted(globmod.glob("/tmp/claude_tokens/auth_state_*.json"))
        if not state_files:
            print("No saved auth states found in /tmp/claude_tokens/")
            sys.exit(1)
        for sf in state_files:
            with open(sf) as f:
                state = json.load(f)
            age = time.time() - state["timestamp"]
            email_addr = state["email"]
            print(f"\n{email_addr} (auth code age: {int(age)}s)")
            if age > 600:
                print(f"  SKIP: Auth code too old ({int(age)}s > 600s)")
                continue
            try:
                token_resp = _http_json(
                    "https://console.anthropic.com/v1/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": state["auth_code"],
                        "redirect_uri": REDIRECT_URI,
                        "client_id": CLIENT_ID,
                        "code_verifier": state["code_verifier"],
                    },
                )
                if "access_token" in token_resp:
                    creds = {
                        "claudeAiOauth": {
                            "accessToken": token_resp["access_token"],
                            "refreshToken": token_resp.get("refresh_token", ""),
                            "expiresAt": int((time.time() + token_resp.get("expires_in", 43200)) * 1000),
                            "scopes": SCOPES.split(),
                        }
                    }
                    out = os.path.join(args.output_dir, f"{email_addr.replace('@','_at_')}.json")
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "w") as f2:
                        json.dump(creds, f2, indent=2)
                    os.chmod(out, 0o600)
                    print(f"  SUCCESS — saved: {out}")
                    os.remove(sf)
                else:
                    print(f"  FAILED: {str(token_resp)[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")
        return

    if args.all:
        # AUDIT-FIX C-1: --all needs built-in accounts; error clearly if unconfigured.
        if not ACCOUNTS:
            sys.exit(
                "No accounts configured for --all: set CLAUDE_SEED_ACCOUNTS_JSON "
                "(JSON array) or CLAUDE_SEED_ACCOUNTS_FILE (path to JSON array)"
            )
        if args.dry_run:
            print("[Pipeline] Dry run: registering mail accounts only")
            register_mail_accounts(ACCOUNTS)
            return

        results = run_pipeline(
            accounts=ACCOUNTS,
            output_dir=args.output_dir,
            register_mail=args.register_mail,
        )

        if args.import_to_saas:
            print(f"\n[Pipeline] Importing tokens to SaaS: {args.import_to_saas}")
            import_tokens_to_saas(args.output_dir, args.import_to_saas)

        return results
    elif args.email and args.password:
        accts = [{"email": args.email, "password": args.password}]
        mail_tokens = {}
        if args.register_mail:
            mail_tokens = register_mail_accounts(accts)

        creds = do_oauth_login(
            email=args.email,
            password=args.password,
            mail_token=args.mail_token or mail_tokens.get(args.email),
            output_path=args.output or f"/tmp/claude_tokens/{args.email.split('@')[0]}.json",
        )
        if creds:
            # AUDIT-FIX C-2: do not print token body to stdout (lands in container logs).
            # creds are still persisted to the --output file by do_oauth_login().
            expires = creds.get("claudeAiOauth", {}).get("expiresAt", "?")
            print(f"SUCCESS email={args.email} expires={expires}")
        else:
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
