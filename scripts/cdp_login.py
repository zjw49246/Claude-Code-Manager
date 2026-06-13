"""Chrome CDP 登录模块（从 auto_login.py 调用）。"""
import asyncio, json, os, re, select, subprocess, sys, time
from urllib.parse import parse_qs, urlparse
import httpx, websockets

MAILCATCHER = "https://mail.claude-code-manager.com"
CDP_PORT = 9222

async def cdp_eval(ws, expr, timeout=10):
    mid = int(time.time()*1000) % 100000
    await ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg.get("result", {}).get("result", {}).get("value")
        except asyncio.TimeoutError:
            continue
    return None

async def xdotool_click(x, y):
    p = await asyncio.create_subprocess_exec("xdotool", "mousemove", str(x), str(y), "click", "1",
        env={**os.environ}, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await p.wait()

async def handle_cf(ws, ctx, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        title = await cdp_eval(ws, "document.title") or ""
        if "just a moment" not in title.lower():
            print(f"  CF cleared: {ctx}")
            return True
        print(f"  CF challenge: {ctx}, clicking...")
        await xdotool_click(257, 476)
        await asyncio.sleep(5)
    return False

async def cdp_login(email: str, token: str, config_dir: str, oauth_url: str, cookies_171: list[dict] | None = None) -> dict | None:
    # 1. Kill old chrome
    subprocess.run(["pkill", "-f", "chrome.*remote-debugging"], capture_output=True)
    await asyncio.sleep(1)

    # 2. Launch Chrome
    chrome = subprocess.Popen(["google-chrome", "--no-sandbox", "--disable-gpu",
        "--no-first-run", "--disable-extensions", "--window-size=1365,900",
        f"--remote-debugging-port={CDP_PORT}", "--user-data-dir=/tmp/chrome-test-login",
        "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ)
    await asyncio.sleep(4)
    print(f"Chrome pid={chrome.pid}")

    # 3. Connect CDP
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{CDP_PORT}/json")
        tabs = r.json()
    ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")

    try:
        async with websockets.connect(ws_url, max_size=10_000_000) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            await ws.send(json.dumps({"id": 0, "method": "Network.enable"}))
            await asyncio.sleep(0.5)

            # 4. Login page
            await ws.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": "https://claude.ai/login"}}))
            await asyncio.sleep(3)
            await handle_cf(ws, "login")
            await asyncio.sleep(2)

            # 5. Enter email
            JS_SET = """(function(){{var inputs=[...document.querySelectorAll('input[type={type}]')].filter(i=>i.offsetParent!==null);if(!inputs.length)return 'no input';var inp=inputs[0];var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(inp,'{value}');inp.dispatchEvent(new Event('input',{{bubbles:true}}));inp.dispatchEvent(new Event('change',{{bubbles:true}}));return 'set'}})()"""
            JS_BTN = """(function(){{var btns=[...document.querySelectorAll('button')].filter(b=>b.offsetParent!==null);for(var b of btns){{var t=b.textContent.trim();if({cond}){{b.click();return 'clicked:'+t}}}}return 'no match'}})()"""
            r = await cdp_eval(ws, JS_SET.format(type="email", value=email))
            print(f"  Email: {r}")
            await asyncio.sleep(0.5)
            r = await cdp_eval(ws, JS_BTN.format(cond="t.includes('Continue with email')"))
            print(f"  Button: {r}")
            await asyncio.sleep(3)

            # 6. Poll MailCatcher
            send_ts = time.time()
            print("  Polling MailCatcher...")
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as mc:
                deadline = time.time() + 120
                while time.time() < deadline:
                    r = await mc.get(f"{MAILCATCHER}/api/v1/message", params={"token": token, "type": "claude"})
                    d = r.json().get("data", {})
                    subj = d.get("subject", "")
                    link = d.get("code", "")
                    if link.startswith("http") and subj:
                        m = re.search(r"\|\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", subj)
                        if m:
                            t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                            if t >= send_ts - 10:
                                print(f"  Got magic link ({len(link)} chars)")
                                break
                    await asyncio.sleep(2)
                else:
                    print("  TIMEOUT waiting for magic link")
                    return

            # 7. Navigate magic link
            await ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": link}}))
            await asyncio.sleep(3)
            await handle_cf(ws, "magic-link")
            for _ in range(15):
                url = await cdp_eval(ws, "document.location.href") or ""
                if "magic-link" not in url: break
                await asyncio.sleep(2)
            await asyncio.sleep(3)
            print(f"  After magic link: {(await cdp_eval(ws, 'document.location.href') or '')[:60]}")

            # 8. Launch CLI
            cli = subprocess.Popen(["claude", "auth", "login", "--email", email],
                env={"CLAUDE_config_dir": config_dir, "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
            print(f"  CLI pid={cli.pid}")

            # Read OAuth URL
            oauth_url = None
            captured = b""
            dl = time.time() + 15
            while time.time() < dl:
                if cli.poll() is not None: break
                rl, _, _ = select.select([cli.stdout], [], [], 0.2)
                if rl:
                    try: captured += os.read(cli.stdout.fileno(), 8192)
                    except: break
                m = re.search(rb"(https://claude\.com/cai/oauth/authorize\S+)", captured)
                if m: oauth_url = m.group(1).decode(); break
                await asyncio.sleep(0.1)
            if not oauth_url:
                cli.kill(); print("  NO OAuth URL"); return
            print(f"  OAuth URL ({len(oauth_url)} chars)")

            # 9. Navigate to OAuth URL
            await ws.send(json.dumps({"id": 10, "method": "Page.navigate", "params": {"url": oauth_url}}))
            await asyncio.sleep(3)
            await handle_cf(ws, "OAuth")
            await asyncio.sleep(8)

            # 10. Authorize API
            JS_ORG = """(function(){var btn=[...document.querySelectorAll("button")].find(b=>b.textContent.trim()==="Authorize");if(!btn)return null;var fk=Object.keys(btn).find(k=>k.startsWith("__reactFiber"));if(!fk)return null;var c=btn[fk];for(var i=0;i<30&&c;i++){if(c.memoizedState){var s=c.memoizedState;var x=0;while(s&&x<20){var v=s.memoizedState;if(v&&Array.isArray(v)){for(var it of v){if(it&&it.email_address)return(it.memberships&&it.memberships[0]&&it.memberships[0].organization)?it.memberships[0].organization.uuid:null;if(Array.isArray(it)){for(var sub of it){if(sub&&sub.email_address)return(sub.memberships&&sub.memberships[0]&&sub.memberships[0].organization)?sub.memberships[0].organization.uuid:null;}}}}s=s.next;x++;}}c=c.return;}return null;})()"""
            org = await cdp_eval(ws, JS_ORG)
            print(f"  Org: {org}")
            if org:
                params = {k:v[0] for k,v in parse_qs(urlparse(oauth_url).query).items()}
                body = json.dumps({"response_type":"code","client_id":params.get("client_id",""),"organization_uuid":org,"redirect_uri":params.get("redirect_uri",""),"scope":" ".join(s for s in params.get("scope","").split() if s!="org:create_api_key"),"state":params.get("state",""),"code_challenge":params.get("code_challenge",""),"code_challenge_method":"S256"})
                js = f"""(async function(){{var r=await fetch("/v1/oauth/{org}/authorize",{{method:"POST",headers:{{"Content-Type":"application/json"}},credentials:"include",body:{json.dumps(body)}}});return r.status+" | "+await r.text()}})()"""
                result = await cdp_eval(ws, js, timeout=15)
                print(f"  Authorize: {(result or '')[:120]}")
                if result and result.startswith("200"):
                    _, txt = result.split(" | ", 1)
                    rd = json.loads(txt).get("redirect_uri","")
                    cp = parse_qs(urlparse(rd).query)
                    code, state = cp.get("code",[""])[0], cp.get("state",[""])[0]
                    if code and state:
                        code_state = f"{code}#{state}"
                        print(f"  Feeding code#state to CLI stdin...")
                        cli.stdin.write(f"{code_state}\n".encode())
                        cli.stdin.flush()
                        cli.stdin.close()
                        for _ in range(30):
                            if cli.poll() is not None: break
                            await asyncio.sleep(1)
                        r = subprocess.run(["claude","auth","status","--text"],
                            env={"CLAUDE_config_dir":config_dir,"PATH":os.environ["PATH"]},
                            capture_output=True, text=True, timeout=15)
                        print(f"  Auth status: {r.stdout.strip()[:200]}")
                        if email.lower() in r.stdout.lower():
                            print("SUCCESS!")
                        else:
                            print("FAILED: auth status doesn't show email")
                        return
            print("FAILED: authorize")
            return None
    finally:
        chrome.kill(); chrome.wait()

# called from auto_login.py
