"""
browser_automation.py — Playwright-powered headful browser automation for account-nuker.

Features:
  • Headful Chromium (headless=False) — real GUI browser visible on screen
  • Human-like typing, mouse movement, and random delays (1-3 s stealth)
  • CAPTCHA detection (reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, image CAPTCHAs)
  • CAPTCHA pause-and-resume: freezes automation, prompts user to solve manually, then resumes
  • Smart form finder: email, password, delete/confirm/unsubscribe fields
  • Multi-step flow support (login → settings → delete)
  • Session state persisted to ~/.account-nuker/sessions/<domain>.json
  • Per-domain deletion result logged to ~/account-nuker.log
  • X11/Wayland compatible (sets DISPLAY / WAYLAND_DISPLAY automatically)
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("account-nuker.browser")

# ── Playwright auto-install ───────────────────────────────────────────────────
def ensure_playwright():
    """Install playwright + chromium if not already present."""
    import subprocess, importlib
    try:
        importlib.import_module("playwright")
    except ImportError:
        print("[account-nuker] Installing playwright…")
        for flag in [["--break-system-packages"], ["--user"]]:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + flag + ["playwright"],
                capture_output=True,
            )
            if r.returncode == 0:
                break
        else:
            raise RuntimeError("Could not install playwright. Run: pip install playwright")

    # Check if Chromium browsers are installed
    browsers_path = Path.home() / ".cache" / "ms-playwright"
    if not browsers_path.exists() or not any(browsers_path.glob("chromium-*")):
        print("[account-nuker] Installing Playwright Chromium (one-time, ~150 MB)…")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium",
             "--with-deps"],
            check=False,
        )


# ── Display environment helpers ───────────────────────────────────────────────
def _ensure_display():
    """
    Make sure a display variable is set for headful browser on Linux.
    Prefers Wayland, falls back to X11 :0, warns if neither is available.
    """
    if os.environ.get("WAYLAND_DISPLAY"):
        return  # Wayland session active

    if not os.environ.get("DISPLAY"):
        # Try common X11 displays
        for disp in (":0", ":1", ":10"):
            test = subprocess.run(
                ["xdpyinfo", "-display", disp],
                capture_output=True, timeout=2
            )
            if test.returncode == 0:
                os.environ["DISPLAY"] = disp
                log.info("Set DISPLAY=%s", disp)
                return

        # Last resort: check if running inside Xvfb or VNC
        if Path("/tmp/.X11-unix").exists():
            sockets = list(Path("/tmp/.X11-unix").glob("X*"))
            if sockets:
                num = sockets[0].name[1:]
                os.environ["DISPLAY"] = f":{num}"
                log.info("Found X socket, set DISPLAY=:%s", num)
                return

        log.warning(
            "No DISPLAY or WAYLAND_DISPLAY found. "
            "Headful browser may fail. Start an X11 session or run with DISPLAY=:0"
        )


import subprocess  # needed by _ensure_display above; import here too


# ── CAPTCHA signature database ────────────────────────────────────────────────
CAPTCHA_IFRAME_DOMAINS = [
    "recaptcha.net",
    "google.com/recaptcha",
    "hcaptcha.com",
    "challenges.cloudflare.com",  # Turnstile
    "arkoselabs.com",             # FunCaptcha / Arkose
    "geetest.com",
    "captcha.com",
    "botdetect.com",
    "solvemedia.com",
]

CAPTCHA_JS_PATTERNS = [
    r"grecaptcha",
    r"hcaptcha",
    r"turnstile\.render",
    r"cf-turnstile",
    r"ArkoseLabs",
    r"geetest",
    r"initGeetest",
]

CAPTCHA_DOM_SELECTORS = [
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "#captcha",
    "[data-captcha]",
    "[data-sitekey]",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='cloudflare']",
    "iframe[src*='arkoselabs']",
    "img[src*='captcha']",
    "input[name*='captcha']",
    "input[id*='captcha']",
    "[class*='captcha']",
    "[id*='captcha']",
]

CAPTCHA_TEXT_PATTERNS = [
    "i'm not a robot",
    "prove you're human",
    "security check",
    "verify you are human",
    "complete the captcha",
    "enter the characters",
    "type the text",
    "solve the puzzle",
]

# ── Form field heuristics ─────────────────────────────────────────────────────
EMAIL_SELECTORS = [
    "input[type='email']",
    "input[name*='email']",
    "input[id*='email']",
    "input[placeholder*='email' i]",
    "input[autocomplete='email']",
    "input[autocomplete='username']",
]

PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name*='password']",
    "input[id*='password']",
    "input[placeholder*='password' i]",
]

DELETE_BUTTON_SELECTORS = [
    "button:has-text('Delete Account')",
    "button:has-text('Delete my account')",
    "button:has-text('Close Account')",
    "button:has-text('Deactivate')",
    "button:has-text('Remove Account')",
    "button:has-text('Cancel Membership')",
    "button:has-text('Unsubscribe')",
    "a:has-text('Delete Account')",
    "a:has-text('Close Account')",
    "a:has-text('Deactivate Account')",
    "[data-testid*='delete']",
    "[data-testid*='deactivate']",
    "[aria-label*='delete account' i]",
]

CONFIRM_SELECTORS = [
    "button:has-text('Confirm')",
    "button:has-text('Yes, delete')",
    "button:has-text('Yes, close')",
    "button:has-text('I understand')",
    "button:has-text('Continue')",
    "button:has-text('Proceed')",
    "[data-testid*='confirm']",
]

SETTINGS_LINK_SELECTORS = [
    "a:has-text('Settings')",
    "a:has-text('Account')",
    "a:has-text('Profile')",
    "a[href*='settings']",
    "a[href*='account']",
    "a[href*='profile']",
    "[aria-label*='settings' i]",
    "[aria-label*='account' i]",
]


# ── Human behaviour helpers ───────────────────────────────────────────────────
async def human_delay(min_ms: int = 800, max_ms: int = 2800):
    """Random pause mimicking human reaction/reading time."""
    ms = random.randint(min_ms, max_ms)
    await asyncio.sleep(ms / 1000)


async def human_type(page, selector: str, text: str, clear_first: bool = True):
    """
    Type text into a field character-by-character with randomised delays
    and occasional brief pauses to simulate natural human typing.
    """
    el = page.locator(selector).first
    await el.wait_for(state="visible", timeout=8000)
    await el.click()
    await asyncio.sleep(random.uniform(0.2, 0.6))

    if clear_first:
        await el.triple_click()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await el.fill("")

    for i, char in enumerate(text):
        await el.type(char, delay=random.randint(40, 180))
        # Occasional natural pause mid-word
        if random.random() < 0.04:
            await asyncio.sleep(random.uniform(0.3, 0.9))
    await asyncio.sleep(random.uniform(0.2, 0.5))


async def human_click(page, locator, jitter: bool = True):
    """
    Click a locator with a small random pixel offset for naturalness.
    Moves the mouse to the element first.
    """
    el = locator.first
    await el.wait_for(state="visible", timeout=8000)

    box = await el.bounding_box()
    if box and jitter:
        x = box["x"] + box["width"]  / 2 + random.randint(-4, 4)
        y = box["y"] + box["height"] / 2 + random.randint(-3, 3)
        await page.mouse.move(x, y, steps=random.randint(8, 20))
        await asyncio.sleep(random.uniform(0.05, 0.2))

    await el.click()
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def random_mouse_wander(page):
    """Briefly move the mouse around to look less robotic."""
    vp = page.viewport_size or {"width": 1280, "height": 800}
    for _ in range(random.randint(2, 5)):
        x = random.randint(100, vp["width"]  - 100)
        y = random.randint(100, vp["height"] - 100)
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.05, 0.25))


# ── CAPTCHA detection ─────────────────────────────────────────────────────────
async def detect_captcha(page) -> dict:
    """
    Scan the page for CAPTCHA signals.
    Returns {detected: bool, type: str, description: str}.
    """
    result = {"detected": False, "type": "none", "description": ""}

    # 1. DOM selector check
    for sel in CAPTCHA_DOM_SELECTORS:
        try:
            count = await page.locator(sel).count()
            if count > 0:
                result.update({
                    "detected": True,
                    "type": "dom",
                    "description": f"CAPTCHA element found: {sel}",
                })
                return result
        except Exception:
            pass

    # 2. iframe src check
    try:
        frames = page.frames
        for frame in frames:
            url = frame.url or ""
            for pattern in CAPTCHA_IFRAME_DOMAINS:
                if pattern in url:
                    result.update({
                        "detected": True,
                        "type": "iframe",
                        "description": f"CAPTCHA iframe: {url[:80]}",
                    })
                    return result
    except Exception:
        pass

    # 3. JavaScript global check
    try:
        js_content = await page.evaluate("""() => document.documentElement.innerHTML""")
        for pattern in CAPTCHA_JS_PATTERNS:
            if re.search(pattern, js_content, re.IGNORECASE):
                result.update({
                    "detected": True,
                    "type": "js",
                    "description": f"CAPTCHA JS signature: {pattern}",
                })
                return result
    except Exception:
        pass

    # 4. Visible text check
    try:
        body_text = await page.evaluate("() => document.body.innerText.toLowerCase()")
        for phrase in CAPTCHA_TEXT_PATTERNS:
            if phrase in body_text:
                result.update({
                    "detected": True,
                    "type": "text",
                    "description": f"CAPTCHA text: '{phrase}'",
                })
                return result
    except Exception:
        pass

    return result


async def wait_for_captcha_solve(page, console_ref=None) -> bool:
    """
    Pause automation and wait for the user to solve the CAPTCHA manually.
    Monitors the page for:
      - The CAPTCHA disappearing from DOM
      - A navigation away from the blocked page
      - User pressing ENTER in the terminal

    Returns True when solved, False on timeout.
    """
    msg = (
        "\n┌─────────────────────────────────────────────────────────┐\n"
        "│  🛑  CAPTCHA DETECTED — Human solve required             │\n"
        "│                                                           │\n"
        "│  1. Look at the browser window that just opened          │\n"
        "│  2. Solve the CAPTCHA challenge manually                 │\n"
        "│  3. Press ENTER here when done (or the page will auto-   │\n"
        "│     detect when it's solved)                             │\n"
        "└─────────────────────────────────────────────────────────┘\n"
    )
    print(msg, flush=True)
    log.info("CAPTCHA detected — waiting for manual solve")

    solved = asyncio.Event()
    start = time.time()
    timeout = 300  # 5 min max

    # Watch for CAPTCHA disappearing or page navigation
    async def poll_solved():
        while not solved.is_set() and (time.time() - start) < timeout:
            await asyncio.sleep(2)
            try:
                cap = await detect_captcha(page)
                if not cap["detected"]:
                    log.info("CAPTCHA appears solved (no longer detected)")
                    solved.set()
                    return
                # Also check if a form submit happened (URL changed)
            except Exception:
                pass

    # Accept ENTER from terminal as manual "done"
    loop = asyncio.get_event_loop()

    def stdin_ready():
        try:
            line = sys.stdin.readline()
            if line is not None:
                solved.set()
        except Exception:
            pass

    try:
        loop.add_reader(sys.stdin.fileno(), stdin_ready)
    except Exception:
        pass  # stdin may not be a real tty in all environments

    poll_task = asyncio.create_task(poll_solved())
    try:
        await asyncio.wait_for(solved.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print("\n⏱  CAPTCHA timeout — skipping this account.", flush=True)
        log.warning("CAPTCHA solve timed out")
        poll_task.cancel()
        return False
    finally:
        try:
            loop.remove_reader(sys.stdin.fileno())
        except Exception:
            pass
        poll_task.cancel()

    print("✓  Resuming automation…\n", flush=True)
    await human_delay(1200, 2500)  # brief pause before resuming
    return True


# ── Session persistence ───────────────────────────────────────────────────────
class SessionStore:
    def __init__(self, app_dir: Path):
        self.store_dir = app_dir / "sessions"
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, domain: str) -> Path:
        safe = re.sub(r"[^\w.\-]", "_", domain)
        return self.store_dir / f"{safe}.json"

    def save(self, domain: str, cookies: list, local_storage: dict = None):
        data = {
            "domain": domain,
            "cookies": cookies,
            "local_storage": local_storage or {},
            "saved_at": time.time(),
        }
        self._path(domain).write_text(json.dumps(data, indent=2))

    def load(self, domain: str) -> Optional[dict]:
        p = self._path(domain)
        if p.exists():
            try:
                d = json.loads(p.read_text())
                # Expire sessions older than 7 days
                if time.time() - d.get("saved_at", 0) < 86400 * 7:
                    return d
            except Exception:
                pass
        return None

    def clear(self, domain: str):
        p = self._path(domain)
        if p.exists():
            p.unlink()


# ── Main automation class ─────────────────────────────────────────────────────
class AccountDeleter:
    """
    Orchestrates Playwright-based account deletion for a single service.
    """

    def __init__(self, app_dir: Path, headless: bool = False):
        self.app_dir  = app_dir
        self.headless = headless
        self.sessions = SessionStore(app_dir)
        self._browser  = None
        self._context  = None
        self._page     = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def _launch(self):
        from playwright.async_api import async_playwright
        _ensure_display()

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # stealth
                "--disable-infobars",
                "--start-maximized",
            ],
            slow_mo=random.randint(60, 120),  # global base slowdown
        )

    async def _new_context(self, domain: str):
        """Create a context, restoring saved session if available."""
        saved = self.sessions.load(domain)

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
            # Disable WebDriver flag
            java_script_enabled=True,
        )

        # Stealth: override navigator.webdriver
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            window.chrome = { runtime: {} };
        """)

        if saved and saved.get("cookies"):
            try:
                await self._context.add_cookies(saved["cookies"])
                log.info("Restored %d cookies for %s", len(saved["cookies"]), domain)
            except Exception as e:
                log.warning("Cookie restore failed for %s: %s", domain, e)

        self._page = await self._context.new_page()

        # Intercept and block analytics/tracking to reduce noise
        await self._page.route(
            re.compile(r"(analytics|gtag|googletagmanager|hotjar|mixpanel|segment\.io)"),
            lambda route: route.abort(),
        )

    async def _save_session(self, domain: str):
        try:
            cookies = await self._context.cookies()
            self.sessions.save(domain, cookies)
            log.info("Session saved for %s (%d cookies)", domain, len(cookies))
        except Exception as e:
            log.debug("Could not save session for %s: %s", domain, e)

    async def _teardown(self):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if hasattr(self, "_pw"):
                await self._pw.stop()
        except Exception:
            pass

    # ── CAPTCHA-aware navigation ───────────────────────────────────────────────
    async def _goto(self, url: str, wait_until: str = "domcontentloaded"):
        """Navigate, detect CAPTCHA, pause if needed."""
        log.info("Navigating to %s", url)
        await self._page.goto(url, wait_until=wait_until, timeout=30000)
        await human_delay(800, 2000)

        cap = await detect_captcha(self._page)
        if cap["detected"]:
            log.info("CAPTCHA on %s: %s", url, cap["description"])
            solved = await wait_for_captcha_solve(self._page)
            if not solved:
                raise RuntimeError(f"CAPTCHA not solved on {url}")

        return self._page

    # ── Form helpers ──────────────────────────────────────────────────────────
    async def _fill_email(self, email: str) -> bool:
        for sel in EMAIL_SELECTORS:
            try:
                if await self._page.locator(sel).count() > 0:
                    await human_type(self._page, sel, email)
                    log.debug("Filled email in: %s", sel)
                    return True
            except Exception:
                continue
        return False

    async def _fill_password(self, password: str) -> bool:
        for sel in PASSWORD_SELECTORS:
            try:
                if await self._page.locator(sel).count() > 0:
                    await human_type(self._page, sel, password)
                    log.debug("Filled password in: %s", sel)
                    return True
            except Exception:
                continue
        return False

    async def _click_delete_button(self) -> bool:
        for sel in DELETE_BUTTON_SELECTORS:
            try:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    await human_delay(500, 1500)
                    await human_click(self._page, loc)
                    log.info("Clicked delete button: %s", sel)
                    return True
            except Exception:
                continue
        return False

    async def _click_confirm_button(self) -> bool:
        for sel in CONFIRM_SELECTORS:
            try:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    await human_delay(800, 2000)
                    await human_click(self._page, loc)
                    log.info("Clicked confirm: %s", sel)
                    return True
            except Exception:
                continue
        return False

    async def _navigate_to_settings(self) -> bool:
        for sel in SETTINGS_LINK_SELECTORS:
            try:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    await human_click(self._page, loc)
                    await human_delay(1000, 2500)
                    log.info("Navigated to settings via: %s", sel)
                    return True
            except Exception:
                continue
        return False

    async def _handle_confirmation_dialog(self) -> bool:
        """Handle JS confirm dialogs or modal overlays."""
        # Listen for dialog
        dialog_handled = asyncio.Event()

        async def handle_dialog(dialog):
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await dialog.accept()
            dialog_handled.set()
            log.info("Accepted dialog: %s", dialog.message[:80])

        self._page.once("dialog", handle_dialog)

        # Also try clicking modal confirm buttons
        await asyncio.sleep(1.5)
        modal_selectors = [
            ".modal button:has-text('Delete')",
            ".modal button:has-text('Confirm')",
            ".dialog button:has-text('Yes')",
            "[role='dialog'] button:has-text('Delete')",
            "[role='dialog'] button:has-text('Confirm')",
            "[role='alertdialog'] button:has-text('Confirm')",
        ]
        for sel in modal_selectors:
            try:
                if await self._page.locator(sel).count() > 0:
                    await human_click(self._page, self._page.locator(sel))
                    return True
            except Exception:
                continue

        return dialog_handled.is_set()

    # ── Core deletion flow ────────────────────────────────────────────────────
    async def _attempt_login(self, login_url: str, email: str, password: str) -> bool:
        """Try to log in to a service. Returns True on apparent success."""
        log.info("Attempting login at %s", login_url)
        await self._goto(login_url)
        await random_mouse_wander(self._page)

        email_ok    = await self._fill_email(email)
        if not email_ok:
            log.warning("Could not find email field on %s", login_url)
            return False

        await human_delay(600, 1400)
        pass_ok = await self._fill_password(password)

        # Some sites show password field only after email submit
        if not pass_ok:
            # Try pressing Enter/Next after email
            await self._page.keyboard.press("Enter")
            await human_delay(1200, 2500)
            cap = await detect_captcha(self._page)
            if cap["detected"]:
                solved = await wait_for_captcha_solve(self._page)
                if not solved:
                    return False
            pass_ok = await self._fill_password(password)

        if not pass_ok:
            log.warning("Could not find password field on %s", login_url)
            # Let user handle it manually
            print(
                "\n⚠  Could not auto-fill password. Please log in manually in the browser,\n"
                "   then press ENTER here to continue…",
                flush=True,
            )
            sys.stdin.readline()

        await human_delay(500, 1200)

        # Submit login form
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Sign in')",
            "button:has-text('Login')",
            "button:has-text('Continue')",
        ]
        for sel in submit_selectors:
            try:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    await human_click(self._page, loc)
                    break
            except Exception:
                continue

        await human_delay(1500, 3000)

        # Check for CAPTCHA post-submit
        cap = await detect_captcha(self._page)
        if cap["detected"]:
            solved = await wait_for_captcha_solve(self._page)
            if not solved:
                return False

        # Heuristic: check we're no longer on the login page
        current_url = self._page.url
        logged_in = (
            "login" not in current_url.lower()
            and "signin" not in current_url.lower()
            and "sign-in" not in current_url.lower()
        )
        log.info("Login heuristic: logged_in=%s (now at %s)", logged_in, current_url[:80])
        return logged_in

    async def delete_account(
        self,
        account: dict,
        email: str,
        password: str,
        dry_run: bool = False,
    ) -> dict:
        """
        Full deletion flow for one account entry.

        Returns result dict:
          {success, domain, status, notes, url_visited}
        """
        domain      = account.get("domain", "")
        service     = account.get("name", domain)
        delete_url  = account.get("url", f"https://{domain}")
        difficulty  = account.get("difficulty", "unknown")

        result = {
            "success":     False,
            "domain":      domain,
            "service":     service,
            "status":      "not_attempted",
            "notes":       "",
            "url_visited": delete_url,
        }

        if difficulty == "impossible":
            result["status"] = "impossible"
            result["notes"]  = "JustDeleteMe marks this as impossible"
            log.info("Skipping %s — difficulty=impossible", domain)
            return result

        print(
            f"\n🤖  Automating: {service} ({domain})\n"
            f"    Difficulty : {difficulty.upper()}\n"
            f"    Target URL : {delete_url}\n",
            flush=True,
        )

        if dry_run:
            result.update({"success": True, "status": "dry_run",
                           "notes": "Dry-run — no actions taken"})
            return result

        try:
            await self._launch()
            await self._new_context(domain)

            # ── Step 1: Navigate to deletion page ────────────────────────────
            await self._goto(delete_url)
            await random_mouse_wander(self._page)
            await human_delay(1000, 2500)

            current_url = self._page.url

            # ── Step 2: Check if login is needed ─────────────────────────────
            is_login_page = any(
                kw in current_url.lower()
                for kw in ("login", "signin", "sign-in", "auth", "account/access")
            ) or await self._page.locator("input[type='password']").count() > 0

            if is_login_page:
                log.info("Login page detected — attempting auto-login")
                print("   🔐 Login required — attempting auto-fill…", flush=True)
                login_ok = await self._attempt_login(current_url, email, password)
                if not login_ok:
                    print(
                        "   ⚠  Auto-login may have failed. Please log in manually\n"
                        "      in the browser window, navigate to account deletion,\n"
                        "      then press ENTER here.",
                        flush=True,
                    )
                    sys.stdin.readline()
                await self._save_session(domain)

            # ── Step 3: Try to find and click the delete button ───────────────
            await human_delay(800, 2000)

            # First try direct delete button on current page
            deleted = await self._click_delete_button()

            # If not found, try navigating to settings first
            if not deleted:
                log.info("Delete button not found — trying settings navigation")
                settings_ok = await self._navigate_to_settings()
                if settings_ok:
                    await human_delay(1000, 2500)
                    deleted = await self._click_delete_button()

            if not deleted:
                print(
                    "   ⚠  Could not auto-find delete button.\n"
                    "      Please navigate to the delete/close account option manually,\n"
                    "      then press ENTER here when you've clicked it.",
                    flush=True,
                )
                sys.stdin.readline()
                deleted = True  # Trust the user

            # ── Step 4: Handle confirmation dialog / page ─────────────────────
            if deleted:
                await human_delay(1000, 2500)

                # Check for CAPTCHA on confirmation page
                cap = await detect_captcha(self._page)
                if cap["detected"]:
                    log.info("CAPTCHA on confirmation page: %s", cap["description"])
                    solved = await wait_for_captcha_solve(self._page)
                    if not solved:
                        result["status"] = "captcha_timeout"
                        result["notes"]  = "CAPTCHA not solved in time"
                        return result

                await self._handle_confirmation_dialog()
                await self._click_confirm_button()

                await human_delay(1500, 3000)

                # ── Step 5: Verify deletion ───────────────────────────────────
                final_url   = self._page.url
                page_text   = ""
                try:
                    page_text = (await self._page.evaluate(
                        "() => document.body.innerText"
                    )).lower()
                except Exception:
                    pass

                success_signals = [
                    "deleted", "closed", "removed", "deactivated",
                    "account has been", "successfully", "goodbye",
                    "sorry to see you go", "your account will be",
                ]
                success = any(s in page_text for s in success_signals)

                result["success"]     = success
                result["status"]      = "deleted" if success else "submitted"
                result["url_visited"] = final_url
                result["notes"]       = (
                    "Deletion confirmed on page" if success
                    else "Submitted — check email for confirmation"
                )

                log.info(
                    "Deletion result for %s: success=%s status=%s url=%s",
                    domain, success, result["status"], final_url[:80]
                )

        except Exception as e:
            result["status"] = "error"
            result["notes"]  = str(e)[:200]
            log.error("Automation error for %s: %s", domain, e, exc_info=True)
            print(f"   ✗  Automation error: {e}", flush=True)

        finally:
            await self._teardown()

        return result


# ── Batch runner ──────────────────────────────────────────────────────────────
async def run_batch_deletion(
    accounts: list,
    email: str,
    password: str,
    app_dir: Path,
    dry_run: bool = False,
    headless: bool = False,
    delay_between: tuple = (3, 8),
) -> list:
    """
    Run deletion for a list of account dicts sequentially.
    Returns list of result dicts.
    """
    results = []
    deleter = AccountDeleter(app_dir=app_dir, headless=headless)

    for i, acc in enumerate(accounts, 1):
        print(
            f"\n──── [{i}/{len(accounts)}] {acc.get('name', acc.get('domain'))} ────",
            flush=True
        )
        result = await deleter.delete_account(
            acc, email, password, dry_run=dry_run
        )
        results.append(result)

        # Human-like inter-site delay
        if i < len(accounts):
            wait = random.uniform(*delay_between)
            print(f"   ⏳ Waiting {wait:.1f}s before next site…", flush=True)
            await asyncio.sleep(wait)

    return results


def run_automation(
    accounts: list,
    email: str,
    password: str,
    app_dir: Path,
    dry_run: bool = False,
    headless: bool = False,
) -> list:
    """
    Synchronous entry point — runs the async automation loop.
    Call this from click/rich interactive menu.
    """
    ensure_playwright()
    return asyncio.run(
        run_batch_deletion(
            accounts=accounts,
            email=email,
            password=password,
            app_dir=app_dir,
            dry_run=dry_run,
            headless=headless,
        )
    )
