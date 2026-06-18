#!/usr/bin/env python3
"""
account-nuker GUI — Local web dashboard
Run: python3 gui_app.py  →  opens http://localhost:7734
"""

import sys, subprocess, importlib, importlib.util

REQUIRED = {
    "flask":      "flask",
    "requests":   "requests",
    "bs4":        "beautifulsoup4",
    "imap_tools": "imap-tools",
    "click":      "click",
}

def _ensure_deps():
    missing = [pkg for mod, pkg in REQUIRED.items()
               if not importlib.util.find_spec(mod)]
    if not missing:
        return
    print(f"[account-nuker] Installing: {', '.join(missing)}")
    for flag in [["--break-system-packages"], ["--user"]]:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet"] + flag + missing,
            capture_output=True)
        if r.returncode == 0:
            break

_ensure_deps()

import os, re, json, csv, time, threading, queue, logging, shutil
import imaplib, email as email_lib, getpass, webbrowser, tempfile, sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse
from email.header import decode_header
from typing import Optional
from flask import Flask, Response, request, jsonify, stream_with_context, render_template
import requests

# ── Config ────────────────────────────────────────────────────────────────────
APP_DIR    = Path.home() / ".account-nuker"
CREDS_FILE = APP_DIR / "creds.json"
JDM_CACHE  = APP_DIR / "jdm.json"
LOG_FILE   = Path.home() / "account-nuker.log"
REPORT_CSV = Path.home() / "account-nuker-report.csv"
PORT       = 7734

logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("account-nuker-gui")

# ── Shared state ──────────────────────────────────────────────────────────────
_state = {
    "accounts":    [],
    "scan_running": False,
    "auto_running": False,
    "auto_results": [],
    "log_queue":    queue.Queue(),
}

# ── Re-use logic from app.py / browser_automation.py ─────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
try:
    from app import (fetch_jdm, scan_email, scan_browser_history,
                     match_to_jdm, generate_gdpr_email, save_creds,
                     load_creds, delete_creds, export_csv, NOISE_DOMAINS,
                     IMAP_HOSTS, _app_password_hint)
except ImportError:
    # Inline minimal versions if app.py not present
    NOISE_DOMAINS = {"google.com","googleapis.com","gstatic.com","gmail.com",
                     "yahoo.com","outlook.com","hotmail.com","microsoft.com",
                     "apple.com","icloud.com","cloudflare.com","amazonaws.com"}
    def fetch_jdm(force_refresh=False): return {}
    def scan_email(e, p, dry_run=False): return set()
    def scan_browser_history(): return set()
    def match_to_jdm(d, j): return []
    def generate_gdpr_email(sn, se, ue, dom): return ""
    def save_creds(e, p): pass
    def load_creds(): return None
    def delete_creds(): pass
    def export_csv(a): pass
    def _app_password_hint(e): return ""

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

def _emit(msg: str, kind: str = "log"):
    _state["log_queue"].put({"type": kind, "message": msg,
                              "ts": datetime.now().strftime("%H:%M:%S")})

# ── API routes ────────────────────────────────────────────────────────────────
@app.route("/api/creds", methods=["GET"])
def get_creds():
    stored = load_creds()
    if stored:
        return jsonify({"saved": True, "email": stored[0]})
    return jsonify({"saved": False, "email": ""})

@app.route("/api/creds", methods=["POST"])
def set_creds():
    data = request.json
    save_creds(data["email"], data["password"])
    return jsonify({"ok": True})

@app.route("/api/creds", methods=["DELETE"])
def del_creds():
    delete_creds()
    return jsonify({"ok": True})

@app.route("/api/scan", methods=["POST"])
def start_scan():
    if _state["scan_running"]:
        return jsonify({"error": "Scan already running"}), 400
    data        = request.json or {}
    dry_run     = data.get("dry_run", False)
    email_only  = data.get("email_only", False)
    browser_only= data.get("browser_only", False)
    force_jdm   = data.get("force_jdm", False)
    stored      = load_creds()
    email_addr  = data.get("email") or (stored[0] if stored else "")
    password    = data.get("password") or (stored[1] if stored else "")

    def _run():
        _state["scan_running"] = True
        _state["accounts"] = []
        try:
            _emit("Fetching JustDeleteMe database…", "info")
            jdm = fetch_jdm(force_refresh=force_jdm)
            jdm_count = len(set(v.get("name","") for v in jdm.values() if isinstance(v,dict)))
            if jdm_count > 0:
                src = "online" if force_jdm else "cache/online"
                _emit(f"JDM loaded: {jdm_count} services ({src})", "success")
            else:
                _emit("JDM data unavailable — scan will still work with built-in list", "warn")

            all_domains: set = set()

            if not browser_only and email_addr:
                _emit(f"Connecting to email: {email_addr}…", "info")
                try:
                    ed = scan_email(email_addr, password, dry_run=dry_run)
                    if len(ed) == 0 and not dry_run:
                        _emit("Email scan returned 0 domains — check App Password is correct", "warn")
                    else:
                        _emit(f"Email scan: {len(ed)} domains found", "success")
                    all_domains |= ed
                except Exception as email_err:
                    _emit(f"Email error: {email_err} — check App Password", "error")

            if not email_only:
                _emit("Scanning browser history…", "info")
                bd = scan_browser_history() if not dry_run else {"dryrun-browser.org","spotify.com","netflix.com","twitter.com"}
                _emit(f"Browser scan: {len(bd)} domains found", "success")
                all_domains |= bd

            all_domains -= NOISE_DOMAINS
            _emit(f"Deduped to {len(all_domains)} unique domains", "info")

            _emit("Matching against JustDeleteMe…", "info")
            accounts = match_to_jdm(all_domains, jdm)
            _state["accounts"] = accounts
            _emit(f"Done — {len(accounts)} accounts discovered", "done")
        except Exception as e:
            _emit(f"Scan error: {e}", "error")
            log.exception("Scan error")
        finally:
            _state["scan_running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    return jsonify(_state["accounts"])

@app.route("/api/stream")
def stream():
    def generate():
        while True:
            try:
                item = _state["log_queue"].get(timeout=30)
                yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

@app.route("/api/open", methods=["POST"])
def open_url():
    url = request.json.get("url", "")
    if url:
        webbrowser.open(url)
    return jsonify({"ok": True})

@app.route("/api/gdpr", methods=["POST"])
def gdpr():
    data    = request.json
    stored  = load_creds()
    uemail  = stored[0] if stored else data.get("user_email","user@example.com")
    text    = generate_gdpr_email(
        data.get("name",""), data.get("contact_email",""),
        uemail, data.get("domain",""))
    out = APP_DIR / f"gdpr_{data.get('domain','unknown')}.txt"
    out.write_text(text)
    return jsonify({"text": text, "saved": str(out)})

@app.route("/api/automate", methods=["POST"])
def start_automate():
    if _state["auto_running"]:
        return jsonify({"error": "Automation already running"}), 400
    data      = request.json or {}
    indices   = data.get("indices", [])
    dry_run   = data.get("dry_run", False)
    headless  = data.get("headless", False)
    stored    = load_creds()
    email_addr = stored[0] if stored else ""
    password   = stored[1] if stored else ""
    targets   = [_state["accounts"][i] for i in indices
                 if 0 <= i < len(_state["accounts"])]
    if not targets:
        return jsonify({"error": "No accounts selected"}), 400

    def _run():
        _state["auto_running"]  = True
        _state["auto_results"] = []
        try:
            from browser_automation import run_automation
            _emit(f"Starting automation for {len(targets)} accounts…", "info")
            results = run_automation(
                accounts=targets, email=email_addr, password=password,
                app_dir=APP_DIR, dry_run=dry_run, headless=headless)
            _state["auto_results"] = results
            ok = sum(1 for r in results if r["status"] in ("deleted","submitted","dry_run"))
            _emit(f"Automation complete — {ok}/{len(results)} actioned", "done")
        except Exception as e:
            _emit(f"Automation error: {e}", "error")
            log.exception("Automation error")
        finally:
            _state["auto_running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/auto-results")
def auto_results():
    return jsonify(_state["auto_results"])

@app.route("/api/export")
def export():
    if not _state["accounts"]:
        return jsonify({"error": "No accounts to export"}), 400
    export_csv(_state["accounts"])
    return jsonify({"path": str(REPORT_CSV)})

# ── Main HTML page ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template('index.html')

# ── Launch ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import click as _click

    @_click.command()
    @_click.option("--port", default=PORT, help="Port to serve on")
    @_click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
    def run(port, no_browser):
        """account-nuker GUI — serves a local web dashboard."""
        APP_DIR.mkdir(parents=True, exist_ok=True)
        print(f"\n  ⚡ account-nuker GUI starting…")
        print(f"  → http://localhost:{port}\n")
        if not no_browser:
            threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
        app.run(host="127.0.0.1", port=port, threaded=True, debug=False)

    run()
