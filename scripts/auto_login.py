#!/usr/bin/env python3
"""Standalone Claude account auto-login for Linux servers.

Adapted from agent-ml-research/core/tools/account_login.py.
Stripped macOS keychain logic; uses .credentials.json on Linux.

Dependencies: pip install httpx playwright mitmproxy playwright-stealth
Setup:        playwright install chromium

Usage:
  # Interactive — prompts for email and token
  python3 auto_login.py

  # Direct
  python3 auto_login.py --email user@example.com --token 171MAIL_TOKEN --config-dir ~/.claude-account-3

  # Use saved email_tokens.json
  python3 auto_login.py --email user@example.com --config-dir ~/.claude-account-3

  # Add account to pool after login
  python3 auto_login.py --email user@example.com --config-dir ~/.claude-account-3 --add-to-pool account-3

Email tokens file: ~/.claude-pool/email_tokens.json
  {
    "user@example.com": {"token": "171mail_token_here", "provider": "171mail"}
  }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE_171 = "https://b.171mail.com/api/v1"
API_BASE_MAILCATCHER = "https://mail.claude-code-manager.com"

# 兼容旧代码：默认走 171mail
API_BASE = API_BASE_171
OAUTH_URL_RE = re.compile(r"https://claude\.com/cai/oauth/authorize\?[^\s]+")

_COOKIE_ATTR_KEYS = {"path", "domain", "expires", "max-age", "samesite", "secure", "httponly"}
_DROP_COOKIES = {"__cf_bm", "_cfuvid"}

EMAIL_POLL_TIMEOUT = 300  # mail.com IMAP 拉取可能延迟几分钟

# mail.com 家族域名——这些邮箱走 claude_oauth_login.py（Selenium webmail），
# 其余走 171mail（API 接码）。根据邮箱后缀自动判断，不需要用户手动选 provider。
MAILCOM_DOMAINS = {
    "lovecat.com", "berlin.com", "consultant.com", "birdlover.com",
    "chemist.com", "tvstar.com", "songwriter.net", "mail.com",
    "email.com", "usa.com", "post.com", "europe.com", "asia.com",
    "iname.com", "writeme.com", "dr.com", "cheerful.com",
    "techie.com", "myself.com",
}


def is_mailcom_domain(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    return domain in MAILCOM_DOMAINS
PLAYWRIGHT_NAV_TIMEOUT = 30_000  # ms
CLI_OAUTH_URL_TIMEOUT = 15
CLI_EXIT_TIMEOUT = 30

POOL_DIR = Path.home() / ".claude-pool"
EMAIL_TOKENS_FILE = POOL_DIR / "email_tokens.json"
ACCOUNTS_FILE = POOL_DIR / "accounts.json"


# ---------------------------------------------------------------------------
# Cookie parsing
# ---------------------------------------------------------------------------

def _parse_cookie_header(header: str, default_domain: str = "claude.ai") -> list[dict]:
    cookies: list[dict] = []
    current: dict | None = None
    for seg in header.split("; "):
        name, _, value = seg.partition("=")
        name = name.strip()
        name_l = name.lower()
        if name_l in _COOKIE_ATTR_KEYS:
            if current is None:
                continue
            if name_l == "path":
                current["path"] = value or "/"
            elif name_l == "domain":
                current["domain"] = value.lstrip(".")
            elif name_l == "secure":
                current["secure"] = True
            elif name_l == "httponly":
                current["httpOnly"] = True
            elif name_l == "samesite":
                current["sameSite"] = (value.strip().capitalize() or "Lax")
        else:
            if current is not None:
                cookies.append(current)
            current = {
                "name": name,
                "value": value.strip('"'),
                "domain": default_domain,
                "path": "/",
            }
    if current is not None:
        cookies.append(current)
    return [c for c in cookies if c["name"] not in _DROP_COOKIES and c["value"]]


# ---------------------------------------------------------------------------
# 171mail client
# ---------------------------------------------------------------------------

class MailServiceError(RuntimeError):
    pass


async def _trigger_send(client: httpx.AsyncClient, email: str) -> tuple[str, str]:
    r = await client.post(f"{API_BASE}/claude/send", json={"email": email})
    body = r.json()
    if body.get("code") != 200 or not body.get("data"):
        msg = body.get("error") or body.get("message") or "unknown"
        raise MailServiceError(f"171mail /claude/send failed: {msg}")
    data = body["data"]
    return data["deviceId"], data["clientSha"]


async def _poll_magic_link(
    client: httpx.AsyncClient, token: str, after_ts: float, timeout_s: int
) -> str:
    deadline = time.time() + timeout_s
    last_subject: str | None = None
    while time.time() < deadline:
        r = await client.get(f"{API_BASE}/getClaudeMessage", params={"token": token})
        try:
            payload = r.json()
        except Exception:
            await asyncio.sleep(2)
            continue
        data = payload.get("data") or {}
        subject = data.get("subject") or ""
        if subject and subject != last_subject:
            m = re.search(r"\|\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", subject)
            if m:
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                if t >= after_ts - 5:
                    return data["code"]
            last_subject = subject
        await asyncio.sleep(2)
    raise MailServiceError(f"no fresh magic-link email within {timeout_s}s")


async def _verify_link(
    client: httpx.AsyncClient,
    *,
    link: str,
    device_id: str,
    client_sha: str,
    email: str,
) -> tuple[str, str]:
    r = await client.post(
        f"{API_BASE}/claude/verify",
        json={
            "link": link,
            "info": {"deviceId": device_id, "clientSha": client_sha, "email": email},
        },
    )
    body = r.json()
    if "data" not in body or not body["data"]:
        msg = body.get("error") or body.get("message") or "unknown"
        raise MailServiceError(f"171mail /claude/verify failed: {msg}")
    return body["data"]["cookie"], body["data"]["sessionKey"]


# ---------------------------------------------------------------------------
# MailCatcher client (mail.claude-code-manager.com)
# ---------------------------------------------------------------------------

async def _poll_magic_link_mailcatcher(
    client: httpx.AsyncClient, token: str, after_ts: float, timeout_s: int
) -> str:
    """从 MailCatcher (mail.claude-code-manager.com) 轮询 magic link。"""
    deadline = time.time() + timeout_s
    last_subject: str | None = None
    while time.time() < deadline:
        r = await client.get(
            f"{API_BASE_MAILCATCHER}/api/v1/message",
            params={"token": token, "type": "claude"},
        )
        try:
            payload = r.json()
        except Exception:
            await asyncio.sleep(2)
            continue
        if payload.get("code") != 200 or payload.get("message") != "success":
            await asyncio.sleep(2)
            continue
        data = payload.get("data") or {}
        subject = data.get("subject") or ""
        magic_link = data.get("code") or ""
        if not magic_link or not subject:
            await asyncio.sleep(2)
            continue
        if subject != last_subject:
            # 检查时间戳——只要比 after_ts 新的
            m = re.search(r"\|\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", subject)
            if m:
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                if t >= after_ts - 5:
                    return magic_link
            last_subject = subject
        await asyncio.sleep(2)
    raise MailServiceError(f"MailCatcher: no fresh magic-link email within {timeout_s}s")



# ---------------------------------------------------------------------------
# mail.com Web 读邮件（绕开 IMAP，直接 Web 登录读收件箱拿 magic link）
# ---------------------------------------------------------------------------

async def _poll_magic_link_mailcom(
    email_addr: str, email_password: str, after_ts: float, timeout_s: int
) -> str:
    """mail.com Web 登录 → 读收件箱 → 找最新 Claude magic link。"""
    import httpx as _httpx

    BASE = "https://lightmailer.mail.com"
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    deadline = time.time() + timeout_s
    baseline_mid = 0  # 第一轮记录现有最大 mailId，之后只接受新的

    while time.time() < deadline:
        try:
            c = _httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
            # Login
            home = c.get("https://www.mail.com/")
            stats_m = re.search(r'name="statistics"\s*value="([^"]*)"', home.text)
            stats = stats_m.group(1) if stats_m else ""
            r = c.post("https://login.mail.com/login", data={
                "service": "mailint", "statistics": stats, "uasServiceID": "mc_starter_mailcom",
                "successURL": "https://$(clientName)-$(dataCenter).mail.com/login",
                "loginFailedURL": "https://www.mail.com/logout/?ls=wd",
                "loginErrorURL": "https://www.mail.com/logout/?ls=te",
                "edition": "us", "lang": "en", "usertype": "standard",
                "username": email_addr, "password": email_password,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=True)

            ott = ""
            for rr in r.history:
                m = re.search(r'ott=([^&"]+)', str(rr.headers.get("location", "")))
                if m: ott = m.group(1); break
            if not ott:
                raise MailServiceError("mail.com Web 登录失败（密码错误或被阻止）")

            c.get(f"{BASE}/start?device=desktop&ott={ott}")
            r2 = c.get(f"{BASE}/start?0-1.0-&device=desktop",
                       headers={"Wicket-Ajax": "true", "Wicket-Ajax-BaseURL": "start?0&device=desktop"})
            rpath_m = re.search(r'<redirect><!\[CDATA\[\./([^\]]*)\]\]>', r2.text)
            if not rpath_m:
                raise MailServiceError("mail.com 无法初始化邮箱会话")
            r3 = c.get(f"{BASE}/{rpath_m.group(1)}")
            inbox_m = re.search(r'folderId=(\d+)[^>]*data-webdriver="INBOX', r3.text)
            if not inbox_m:
                raise MailServiceError("mail.com 找不到收件箱")
            fid = inbox_m.group(1)

            r4 = c.get(f"{BASE}/messagelist?folderId={fid}")
            links = re.findall(r'messagedetail\?folderId=\d+&(?:amp;)?mailIndex=\d+&(?:amp;)?mailId=\d+', r4.text)
            subjects = re.findall(r'mail-header__subject">([^<]*)', r4.text)

            # 第一次扫描记录现有最大 mailId，后续只要 mailId 更大的
            for subj, link in zip(subjects, links):
                if "claude" not in subj.lower(): continue
                mid_m = re.search(r"mailId=(\d+)", link)
                mid = int(mid_m.group(1)) if mid_m else 0
                logger.info("mailcom: found claude email mid=%d baseline=%d subj=%s", mid, baseline_mid, subj.strip()[:60])
                if baseline_mid == 0:
                    # 第一轮：记录当前最大 mid 作为基线，跳过所有现有邮件
                    baseline_mid = max(baseline_mid, mid)
                    continue
                if mid <= baseline_mid: continue
                mid = re.search(r'mailId=(\d+)', link).group(1)
                r6 = c.get(f"{BASE}/mailbody/{mid}/false")
                ml = re.findall(r'https://claude\.ai/magic-link[^\s"\'<>]+', r6.text.replace("&amp;","&"))
                if ml:
                    c.close()
                    return ml[0]
            c.close()
        except MailServiceError:
            raise
        except Exception as e:
            logger.warning("mailcom poll attempt failed: %s", e)
        await asyncio.sleep(5)
    raise MailServiceError(f"mail.com Web: 收件箱中 {timeout_s}s 内未找到新的 Claude 登录邮件")

# ---------------------------------------------------------------------------
# mitmproxy (patches CLI 2.1.x OAuth redirect_uri bug)
# ---------------------------------------------------------------------------

_MITM_ADDON = '''
import json
from mitmproxy import http

def request(flow: http.HTTPFlow) -> None:
    if "/v1/oauth/token" not in flow.request.pretty_url:
        return
    body = flow.request.get_text() or ""
    try:
        j = json.loads(body)
    except Exception:
        return
    changed = False
    ru = j.get("redirect_uri", "")
    if ru.startswith("http://localhost") or ru.startswith("http://127.0.0.1"):
        j["redirect_uri"] = "https://platform.claude.com/oauth/code/callback"
        changed = True
    code = j.get("code", "")
    if "#" in code:
        j["code"] = code.split("#", 1)[0]
        changed = True
    if changed:
        flow.request.set_text(json.dumps(j))
'''


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _find_mitmdump() -> str:
    found = shutil.which("mitmdump")
    if found:
        return found
    cand = Path(sys.executable).parent / "mitmdump"
    if cand.exists():
        return str(cand)
    raise FileNotFoundError("mitmdump not found — run: pip install mitmproxy")


async def _start_mitm(work_dir: Path) -> tuple[subprocess.Popen, int, Path]:
    # Ensure CA cert exists
    ca = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    mitm_bin = _find_mitmdump()
    if not ca.exists():
        boot_port = _free_port()
        proc = subprocess.Popen(
            [mitm_bin, "--listen-port", str(boot_port), "-q"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if ca.exists():
                break
            time.sleep(0.1)
        proc.terminate()
        proc.wait(timeout=3)
        if not ca.exists():
            raise RuntimeError("failed to bootstrap mitmproxy CA cert")

    addon_path = work_dir / "_mitm_addon.py"
    addon_path.write_text(_MITM_ADDON)
    port = _free_port()
    proc = subprocess.Popen(
        [mitm_bin, "-s", str(addon_path), "--listen-port", str(port), "-q"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(f"mitmproxy failed to bind port {port}")
    return proc, port, ca


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for cand in [
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".local" / "node" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ]:
        if cand.exists():
            return str(cand)
    raise FileNotFoundError("claude CLI not found")


def _child_pids(pid: int) -> list[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(pid)], text=True, stderr=subprocess.DEVNULL,
        )
        return [int(p) for p in out.split() if p.isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def _discover_listener_port(pid: int, deadline: float) -> int | None:
    while time.time() < deadline:
        candidates = [pid] + _child_pids(pid)
        for cand in candidates:
            try:
                out = subprocess.check_output(
                    ["lsof", "-p", str(cand), "-nP"],
                    text=True, stderr=subprocess.DEVNULL, timeout=2,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                # lsof may not be available; try ss
                try:
                    out = subprocess.check_output(
                        ["ss", "-tlnp"], text=True, stderr=subprocess.DEVNULL, timeout=2,
                    )
                    for line in out.splitlines():
                        if f"pid={cand}" in line:
                            m = re.search(r":(\d+)\s", line)
                            if m:
                                return int(m.group(1))
                except Exception:
                    pass
                continue
            for line in out.splitlines():
                if "LISTEN" not in line:
                    continue
                m = re.search(r"\[?[0-9a-f:.]+\]?:(\d+)\s*\(LISTEN\)", line)
                if m:
                    return int(m.group(1))
        time.sleep(0.2)
    return None


# ---------------------------------------------------------------------------
# Email tokens store
# ---------------------------------------------------------------------------

def load_email_tokens() -> dict:
    if not EMAIL_TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(EMAIL_TOKENS_FILE.read_text())
    except Exception:
        return {}


def save_email_token(email: str, token: str, provider: str = "171mail", mail_password: str = ""):
    data = load_email_tokens()
    entry = {"token": token, "provider": provider}
    if mail_password:
        entry["mail_password"] = mail_password
    data[email] = entry
    EMAIL_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMAIL_TOKENS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.chmod(EMAIL_TOKENS_FILE, 0o600)
    logger.info("saved token for %s to %s", email, EMAIL_TOKENS_FILE)


def get_email_token(email: str) -> str | None:
    data = load_email_tokens()
    entry = data.get(email)
    if not entry:
        for k, v in data.items():
            if k.lower() == email.lower():
                entry = v
                break
    if entry:
        return entry.get("token")
    return None


# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------

def add_to_pool(account_id: str, config_dir: str, email: str):
    data = {"accounts": []}
    if ACCOUNTS_FILE.exists():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text())
        except Exception:
            pass

    # Check if account already exists
    for acc in data["accounts"]:
        if acc["id"] == account_id:
            acc["config_dir"] = config_dir
            acc["email"] = email
            logger.info("updated existing account %s in pool", account_id)
            break
    else:
        data["accounts"].append({
            "id": account_id,
            "config_dir": config_dir,
            "email": email,
            "role": "automation",
            "enabled": True,
        })
        logger.info("added account %s to pool", account_id)

    ACCOUNTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Main login flow
# ---------------------------------------------------------------------------

async def perform_login(
    *,
    email: str,
    token_171: str,
    config_dir: str,
    use_xvfb: bool = True,
) -> bool:
    config_path = Path(config_dir).expanduser()
    config_path.mkdir(parents=True, exist_ok=True)
    work_dir = Path(f"/tmp/claude-login-{uuid.uuid4().hex[:8]}")
    work_dir.mkdir(parents=True, exist_ok=True)

    mitm_proc = None
    try:
        # Step 1: 获取 session cookies（按域名分支）
        cookies: list[dict] = []
        _use_mailcatcher = is_mailcom_domain(email)

        if _use_mailcatcher:
            # mail.com 域：跳过 171mail 的 send/verify——
            # CLI 会自己发邮件，MailCatcher 只负责读邮件拿 magic link。
            # cookies 留空，后续在 Playwright 阶段用 magic link 获取
            logger.info("step 1/5: mail.com 域，跳过 171mail（CLI 自发邮件 + MailCatcher 接码）")
        else:
            # 其他域：171mail 流程
            logger.info("step 1/5: triggering 171mail login email...")
            async with httpx.AsyncClient(timeout=30) as mc:
                device_id, client_sha = await _trigger_send(mc, email)
                send_ts = time.time()
                logger.info("step 2/5: polling for magic link (up to %ds)...", EMAIL_POLL_TIMEOUT)
                magic_link = await _poll_magic_link(mc, token_171, send_ts, EMAIL_POLL_TIMEOUT)
                logger.info("got magic link (%d chars)", len(magic_link))
                cookie_header, session_key = await _verify_link(
                    mc, link=magic_link,
                    device_id=device_id, client_sha=client_sha, email=email,
                )
                logger.info("got sessionKey (%d chars)", len(session_key))

            cookies = _parse_cookie_header(cookie_header)
            logger.info("parsed %d cookies: %s", len(cookies), [c["name"] for c in cookies])

        # Clear stale credentials
        for f in [".claude.json", ".credentials.json"]:
            fp = config_path / f
            if fp.exists():
                fp.unlink()

        # Step 2: Start mitmproxy
        logger.info("step 3/5: starting mitmproxy...")
        mitm_proc, mitm_port, ca_path = await _start_mitm(work_dir)
        logger.info("mitmproxy on :%d", mitm_port)

        # Step 3: Spawn claude auth login
        logger.info("step 4/5: spawning claude auth login...")
        claude_bin = _find_claude()
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(config_path)
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env["HTTPS_PROXY"] = f"http://127.0.0.1:{mitm_port}"
        env["HTTP_PROXY"] = f"http://127.0.0.1:{mitm_port}"
        env["NODE_EXTRA_CA_CERTS"] = str(ca_path)
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

        # Use xvfb-run for display if needed
        if use_xvfb and not os.environ.get("DISPLAY"):
            env["DISPLAY"] = ":99"

        proc = subprocess.Popen(
            [claude_bin, "auth", "login", "--email", email],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        cli_spawn_ts = time.time()
        logger.info("CLI pid=%d (spawn_ts=%.0f)", proc.pid, cli_spawn_ts)

        # Read stdout for OAuth URL
        oauth_url: str | None = None
        captured = b""
        deadline = time.time() + CLI_OAUTH_URL_TIMEOUT
        while time.time() < deadline and oauth_url is None:
            if proc.poll() is not None:
                break
            rlist, _, _ = select.select([proc.stdout], [], [], 0.2)
            if rlist:
                try:
                    captured += os.read(proc.stdout.fileno(), 8192)
                except OSError:
                    break
            m = OAUTH_URL_RE.search(captured.decode(errors="replace"))
            if m:
                oauth_url = m.group(0)
            await asyncio.sleep(0.1)

        if not oauth_url:
            proc.kill()
            snippet = captured.decode(errors="replace")[-400:]
            logger.error("OAuth URL not found. CLI output: %s", snippet)
            return False

        logger.info("OAuth URL found (%d chars)", len(oauth_url))

        # Discover CLI listener port
        listener_port = _discover_listener_port(proc.pid, deadline=time.time() + 5)
        if not listener_port:
            proc.kill()
            logger.error("CLI did not bind a localhost listener")
            return False
        logger.info("CLI listener on port %d", listener_port)

        # Step 4: Playwright — automate OAuth
        logger.info("step 5/5: Playwright browser automation...")
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import Stealth
        except ImportError:
            proc.kill()
            logger.error("playwright/playwright-stealth not installed. Run:")
            logger.error("  pip install playwright playwright-stealth && playwright install chromium")
            return False

        code = state = ""
        xvfb_proc = None
        try:
            # Start Xvfb if no DISPLAY
            if use_xvfb and not os.environ.get("DISPLAY"):
                xvfb_proc = subprocess.Popen(
                    ["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                await asyncio.sleep(0.5)
                os.environ["DISPLAY"] = ":99"

            async with Stealth().use_async(async_playwright()) as pw:
                browser = await pw.chromium.launch(
                    channel="chrome",
                    headless=False,  # headed mode bypasses Cloudflare
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()

                if _use_mailcatcher:
                    # mail.com 域：浏览器模拟登录 Claude → 输入邮箱 → Claude 发验证邮件
                    # → MailCatcher 接码拿 magic link → 浏览器访问 magic link 完成登录
                    # → 拿到 session cookies → 后续 Authorize 和 171mail 一样
                    logger.info("mailcatcher: 浏览器模拟 Claude 登录流程...")

                    # 1. 打开 Claude 登录页
                    await page.goto("https://claude.ai/login", wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT)
                    for _ in range(30):
                        t = (await page.title()) or ""
                        if "just a moment" not in t.lower():
                            break
                        await asyncio.sleep(1)
                    await asyncio.sleep(2)

                    # 2. 点 "Continue with email" 如果有的话
                    try:
                        btn = page.locator("button", has_text="Continue with email").first
                        await btn.wait_for(state="visible", timeout=5000)
                        await btn.click()
                        logger.info("mailcatcher: clicked 'Continue with email'")
                        await asyncio.sleep(2)
                    except Exception:
                        pass

                    # 3. 输入邮箱并提交
                    try:
                        email_input = page.locator('input[type="email"], input[name="email"]').first
                        await email_input.wait_for(state="visible", timeout=5000)
                        await email_input.fill(email)
                        await asyncio.sleep(0.5)
                        submit = page.locator('button[type="submit"]').first
                        await submit.click()
                        logger.info("mailcatcher: submitted email, waiting for verification...")
                        await asyncio.sleep(5)  # 等 Claude 发邮件
                    except Exception as exc:
                        logger.error("mailcatcher: email input failed: %s", exc)

                    # 4. 轮询 MailCatcher 拿 magic link（Claude 此时已发邮件到邮箱）
                    mail_send_ts = time.time()
                    logger.info("mailcatcher: polling MailCatcher for magic link...")
                    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as mc:
                        ml = await _poll_magic_link_mailcatcher(
                            mc, token_171, mail_send_ts, EMAIL_POLL_TIMEOUT
                        )
                    logger.info("mailcatcher: got magic link (%d chars), visiting...", len(ml))

                    # 5. 浏览器访问 magic link 完成登录
                    await page.goto(ml, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT)
                    for _ in range(30):
                        if "claude.ai" in page.url and "magic-link" not in page.url:
                            break
                        await asyncio.sleep(1)
                    logger.info("mailcatcher: logged in, url=%s", page.url[:80])
                else:
                    # 171mail：预注入 cookies
                    await context.add_cookies(cookies)

                # Identity check
                resp = await page.request.get("https://claude.ai/api/account")
                if resp.status == 200:
                    body = json.loads(await resp.text())
                    actual_email = body.get("email_address", "")
                    if actual_email.lower() != email.lower():
                        logger.error("identity mismatch: got %s, expected %s", actual_email, email)
                        await browser.close()
                        proc.kill()
                        return False
                    logger.info("identity confirmed: %s", actual_email)

                # Navigate to OAuth URL
                await page.goto(oauth_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT)

                # Wait past Cloudflare challenge
                cf_deadline = time.time() + 30
                while time.time() < cf_deadline:
                    t = (await page.title()) or ""
                    if "just a moment" not in t.lower() and "challenge" not in t.lower():
                        break
                    await asyncio.sleep(1)

                # Click Authorize
                redirect_chain: list[str] = []
                page.on(
                    "request",
                    lambda r: redirect_chain.append(r.url) if r.is_navigation_request() else None,
                )
                try:
                    btn = page.locator("button", has_text="Authorize").first
                    await btn.wait_for(state="visible", timeout=10000)
                    await btn.click()
                except Exception as exc:
                    logger.error("Authorize click failed: %s", exc)
                    await browser.close()
                    proc.kill()
                    return False

                # Wait for callback
                callback_url: str | None = None
                for _ in range(60):
                    await asyncio.sleep(0.5)
                    for u in redirect_chain:
                        if "/oauth/code/callback" in u or "localhost" in u:
                            callback_url = u
                            break
                    if callback_url:
                        break

                if not callback_url:
                    logger.error("no OAuth callback URL captured")
                    await browser.close()
                    proc.kill()
                    return False

                qs = parse_qs(urlparse(callback_url).query)
                code = qs.get("code", [""])[0]
                state = qs.get("state", [""])[0]
                await browser.close()
        finally:
            if xvfb_proc:
                xvfb_proc.terminate()

        logger.info("got code+state from callback")

        # Deliver to CLI listener
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                url = f"http://localhost:{listener_port}/callback?code={code}&state={state}"
                resp = await cli.get(url, follow_redirects=False)
                logger.info("listener responded %d", resp.status_code)
        except Exception as exc:
            logger.warning("listener delivery error: %s", exc)

        # Wait for CLI to exit
        extra = b""
        for _ in range(CLI_EXIT_TIMEOUT):
            if proc.poll() is not None:
                break
            rlist, _, _ = select.select([proc.stdout], [], [], 1)
            if rlist:
                try:
                    extra += os.read(proc.stdout.fileno(), 8192)
                except OSError:
                    break

        if proc.poll() is None:
            # CLI may hang after successful token exchange; kill and check auth status
            proc.kill()
            logger.warning("CLI did not exit within %ds — killed, checking auth status anyway", CLI_EXIT_TIMEOUT)

        # Verify
        status = subprocess.run(
            [claude_bin, "auth", "status", "--text"],
            env={"CLAUDE_CONFIG_DIR": str(config_path), "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=15,
        )
        if email.lower() in status.stdout.lower():
            logger.info("verified: %s", status.stdout.strip()[:200])
        else:
            logger.warning("auth status did not show email, but login may still be ok")
            logger.warning("status output: %s", status.stdout[:300])

        return True

    except MailServiceError as exc:
        logger.error("171mail error: %s", exc)
        return False
    except Exception as exc:
        logger.error("unexpected error: %s", exc)
        return False
    finally:
        if mitm_proc:
            mitm_proc.terminate()
            try:
                mitm_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                mitm_proc.kill()
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Auto-login Claude account")
    parser.add_argument("--email", help="Claude account email")
    parser.add_argument("--token", help="接码 token（171mail 用）或 mail.com 邮箱密码（mail.com 域自动识别）")
    parser.add_argument("--config-dir", help="CLAUDE_CONFIG_DIR for this account")
    parser.add_argument("--add-to-pool", metavar="ACCOUNT_ID",
                        help="Add to ~/.claude-pool/accounts.json with this ID after login")
    parser.add_argument("--save-token", action="store_true",
                        help="Save the token to email_tokens.json for future use")
    # 兼容旧调用（Worker bootstrap 可能还传这些参数）
    parser.add_argument("--provider", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--mail-password", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    email = args.email
    if not email:
        email = input("Email: ").strip()

    # 按邮箱后缀自动判断登录方式
    use_webmail = is_mailcom_domain(email)

    # token: 171mail 的接码 token，或 mail.com 的邮箱密码
    saved = load_email_tokens().get(email)
    token = args.token or args.mail_password
    if not token and saved:
        token = saved.get("token") or saved.get("mail_password")
        if token:
            logger.info("found saved token for %s", email)
    if not token:
        token = input("mail.com 密码: " if use_webmail else "171mail Token: ").strip()

    config_dir = args.config_dir
    if not config_dir:
        config_dir = input(f"Config dir [{Path.home()}/.claude-account-new]: ").strip()
        if not config_dir:
            config_dir = str(Path.home() / ".claude-account-new")

    if args.save_token or not get_email_token(email):
        provider_label = "mailcom" if use_webmail else "171mail"
        save_email_token(email, token, provider=provider_label)

    # 统一走 perform_login（CLI OAuth），按域名自动选择接码方式
    ok = asyncio.run(perform_login(
        email=email,
        token_171=token,
        config_dir=config_dir,
    ))

    if ok and args.add_to_pool:
        add_to_pool(args.add_to_pool, str(Path(config_dir).expanduser()), email)
        logger.info("account added to pool — restart CCM to pick it up")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
