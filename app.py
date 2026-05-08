#!/usr/bin/env python3
"""
account-nuker — Find and delete your online accounts.
Zero-config, privacy-first, runs locally.
v1.2.0 — Fixed JDM URLs, Gmail All Mail search, Kali browser paths, App Password guidance.
"""

# ── Auto-install deps ─────────────────────────────────────────────────────────
import sys, subprocess, importlib, importlib.util

REQUIRED = {
    "imap_tools": "imap-tools",
    "requests":   "requests",
    "bs4":        "beautifulsoup4",
    "rich":       "rich",
    "click":      "click",
}

def _ensure_deps():
    missing = [pkg for mod, pkg in REQUIRED.items()
               if not importlib.util.find_spec(mod)]
    if not missing:
        return
    print(f"[account-nuker] Installing: {', '.join(missing)}")
    for flag in [["--break-system-packages"], ["--user"], []]:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet"] + flag + missing,
            capture_output=True)
        if r.returncode == 0:
            break
    import site; importlib.invalidate_caches()
    for p in getattr(site, 'getsitepackages', lambda:[])() + [site.getusersitepackages()]:
        if p and p not in sys.path:
            sys.path.insert(0, p)

_ensure_deps()

import os, re, json, csv, time, sqlite3, logging, shutil
import imaplib, email as email_lib, getpass, webbrowser, tempfile
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse
from email.header import decode_header
from typing import Optional

import click
import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.text import Text

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION    = "1.2.0"
APP_DIR    = Path.home() / ".account-nuker"
CREDS_FILE = APP_DIR / "creds.json"
JDM_CACHE  = APP_DIR / "jdm.json"
LOG_FILE   = Path.home() / "account-nuker.log"
REPORT_CSV = Path.home() / "account-nuker-report.csv"
AUTO_LOG   = Path.home() / "account-nuker-automation.json"

# ── FIX 1: Correct JDM URLs (old URLs were 404) ──────────────────────────────
JDM_URLS = [
    # Primary: correct repo name is 'justdeleteme', not 'jdm'
    "https://raw.githubusercontent.com/jdm-contrib/justdeleteme/master/_data/services.json",
    "https://raw.githubusercontent.com/jdm-contrib/justdeleteme/gh-pages/_data/services.json",
    # Website direct
    "https://justdeleteme.xyz/services.json",
    "https://justdeleteme.xyz/data/services.json",
]

IMAP_HOSTS = {
    "gmail.com":      ("imap.gmail.com",        993),
    "googlemail.com": ("imap.gmail.com",        993),
    "outlook.com":    ("imap-mail.outlook.com", 993),
    "hotmail.com":    ("imap-mail.outlook.com", 993),
    "live.com":       ("imap-mail.outlook.com", 993),
    "yahoo.com":      ("imap.mail.yahoo.com",   993),
    "ymail.com":      ("imap.mail.yahoo.com",   993),
    "icloud.com":     ("imap.mail.me.com",      993),
    "me.com":         ("imap.mail.me.com",      993),
}

SEARCH_SUBJECTS = [
    "welcome", "confirm", "verify", "account created",
    "registration", "thanks for signing up", "activate",
    "you're in", "get started", "hello", "account",
    "sign up", "signup", "joined", "membership",
]

DIFFICULTY_COLORS = {
    "easy":       "green",
    "medium":     "yellow",
    "hard":       "red",
    "impossible": "bright_red",
    "unknown":    "dim",
}

# ── FIX 4: Expanded browser paths including Kali Linux ───────────────────────
def _safe_exists(p: Path) -> bool:
    """Path.exists() that catches PermissionError (e.g. /root/ when non-root)."""
    try:
        return p.exists()
    except (PermissionError, OSError):
        return False

def _safe_glob(p: Path, pattern: str) -> list:
    """Path.glob() that catches PermissionError."""
    try:
        return list(p.glob(pattern))
    except (PermissionError, OSError):
        return []

def _get_browser_profiles() -> dict:
    home = Path.home()

    # Gather Firefox profiles safely, skipping inaccessible dirs
    firefox_paths = (
        _safe_glob(home, ".mozilla/firefox/*.default*/places.sqlite") +
        _safe_glob(home, ".mozilla/firefox/*.default/places.sqlite") +
        _safe_glob(Path("/root/.mozilla/firefox"), "*.default*/places.sqlite") +
        _safe_glob(home, "snap/firefox/common/.mozilla/firefox/*.default*/places.sqlite")
    )

    profiles = {
        "Chrome": [
            home / ".config/google-chrome/Default/History",
            home / ".config/google-chrome-beta/Default/History",
            home / ".config/chromium/Default/History",
            home / "snap/chromium/common/chromium/Default/History",
            home / "snap/google-chrome/current/.config/google-chrome/Default/History",
            Path("/root/.config/google-chrome/Default/History"),
            Path("/root/.config/chromium/Default/History"),
        ],
        "Firefox": firefox_paths,
        "Brave": [
            home / ".config/BraveSoftware/Brave-Browser/Default/History",
            Path("/root/.config/BraveSoftware/Brave-Browser/Default/History"),
        ],
        "Edge": [
            home / ".config/microsoft-edge/Default/History",
            home / ".config/microsoft-edge-dev/Default/History",
        ],
        "Opera": [
            home / ".config/opera/History",
        ],
    }
    return profiles

NOISE_DOMAINS = {
    "google.com","googleapis.com","gstatic.com","gmail.com",
    "yahoo.com","outlook.com","hotmail.com","microsoft.com",
    "apple.com","icloud.com","cloudflare.com","amazonaws.com",
    "akamai.com","fastly.com","cdn.net","doubleclick.net",
    "googlesyndication.com","googletagmanager.com","localhost",
    "127.0.0.1","schema.org","w3.org","iana.org","mozilla.org",
    "firefox.com","chrome.google.com","chromium.org",
    "ocsp.sectigo.com","ocsp.digicert.com","ctldl.windowsupdate.com",
}

console = Console()

logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("account-nuker")


# ── Credentials ───────────────────────────────────────────────────────────────
_OBFKEY = b"account-nuker-2024"

def _xor(data: bytes) -> bytes:
    key = _OBFKEY
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def save_creds(email_addr: str, password: str):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"email": email_addr, "password": password}).encode()
    CREDS_FILE.write_bytes(_xor(payload))
    CREDS_FILE.chmod(0o600)
    log.info("Credentials saved.")

def load_creds() -> Optional[tuple]:
    if not CREDS_FILE.exists():
        return None
    try:
        raw = _xor(CREDS_FILE.read_bytes())
        d = json.loads(raw)
        return d["email"], d["password"]
    except Exception:
        return None

def delete_creds():
    if CREDS_FILE.exists():
        CREDS_FILE.unlink()
        console.print("[green]✓ Credentials deleted.[/]")


# ── FIX 1: JustDeleteMe with corrected URLs + hardcoded fallback ──────────────
# Top 120 popular services as an offline fallback when JDM is unreachable
JDM_FALLBACK_SERVICES = [
    {"name":"Facebook","difficulty":"medium","url":"https://www.facebook.com/help/delete_account","domains":["facebook.com"]},
    {"name":"Instagram","difficulty":"medium","url":"https://www.instagram.com/accounts/remove/request/permanent/","domains":["instagram.com"]},
    {"name":"Twitter / X","difficulty":"easy","url":"https://twitter.com/settings/deactivate","domains":["twitter.com","x.com"]},
    {"name":"TikTok","difficulty":"medium","url":"https://www.tiktok.com/setting/","domains":["tiktok.com"]},
    {"name":"Snapchat","difficulty":"easy","url":"https://accounts.snapchat.com/accounts/delete_account","domains":["snapchat.com"]},
    {"name":"LinkedIn","difficulty":"easy","url":"https://www.linkedin.com/psettings/account","domains":["linkedin.com"]},
    {"name":"Reddit","difficulty":"easy","url":"https://www.reddit.com/settings/account","domains":["reddit.com"]},
    {"name":"Pinterest","difficulty":"easy","url":"https://www.pinterest.com/settings/","domains":["pinterest.com"]},
    {"name":"Tumblr","difficulty":"easy","url":"https://www.tumblr.com/account/delete","domains":["tumblr.com"]},
    {"name":"Netflix","difficulty":"medium","url":"https://www.netflix.com/cancelplan","domains":["netflix.com"]},
    {"name":"Spotify","difficulty":"hard","url":"https://support.spotify.com/us/article/close-account/","domains":["spotify.com"]},
    {"name":"Amazon","difficulty":"hard","url":"https://www.amazon.com/hz/contact-us/request-data","domains":["amazon.com","amazon.co.uk"]},
    {"name":"eBay","difficulty":"medium","url":"https://www.ebay.com/help/account/topics/closing-account/closing-ebay-account","domains":["ebay.com"]},
    {"name":"PayPal","difficulty":"hard","url":"https://www.paypal.com/myaccount/closeaccount/","domains":["paypal.com"]},
    {"name":"Dropbox","difficulty":"easy","url":"https://www.dropbox.com/account/delete","domains":["dropbox.com"]},
    {"name":"Adobe","difficulty":"hard","url":"https://account.adobe.com/","domains":["adobe.com"]},
    {"name":"Airbnb","difficulty":"medium","url":"https://www.airbnb.com/users/privacy_settings","domains":["airbnb.com"]},
    {"name":"Uber","difficulty":"hard","url":"https://help.uber.com/riders/article/how-do-i-delete-my-uber-account","domains":["uber.com"]},
    {"name":"Lyft","difficulty":"medium","url":"https://help.lyft.com/hc/en-us/articles/115013080068","domains":["lyft.com"]},
    {"name":"Tinder","difficulty":"easy","url":"https://account.gotinder.com/delete","domains":["tinder.com"]},
    {"name":"Bumble","difficulty":"easy","url":"https://bumble.com/en-us/the-buzz/how-to-delete-bumble","domains":["bumble.com"]},
    {"name":"OKCupid","difficulty":"easy","url":"https://www.okcupid.com/settings","domains":["okcupid.com"]},
    {"name":"Match","difficulty":"hard","url":"https://help.match.com/hc/en-us/articles/6077208541198","domains":["match.com"]},
    {"name":"Hinge","difficulty":"easy","url":"https://hingeapp.zendesk.com/hc/en-us/articles/360013107913","domains":["hinge.co"]},
    {"name":"Discord","difficulty":"easy","url":"https://discord.com/channels/@me","domains":["discord.com"]},
    {"name":"Slack","difficulty":"hard","url":"https://slack.com/help/articles/360000350443","domains":["slack.com"]},
    {"name":"Twitch","difficulty":"easy","url":"https://www.twitch.tv/user/delete-account","domains":["twitch.tv"]},
    {"name":"YouTube","difficulty":"hard","url":"https://myaccount.google.com/deleteaccount","domains":["youtube.com"]},
    {"name":"GitHub","difficulty":"medium","url":"https://github.com/settings/admin","domains":["github.com"]},
    {"name":"GitLab","difficulty":"easy","url":"https://gitlab.com/-/profile/account","domains":["gitlab.com"]},
    {"name":"Stack Overflow","difficulty":"medium","url":"https://stackoverflow.com/users/delete-self","domains":["stackoverflow.com"]},
    {"name":"Medium","difficulty":"easy","url":"https://medium.com/me/settings","domains":["medium.com"]},
    {"name":"Quora","difficulty":"hard","url":"https://www.quora.com/settings/privacy","domains":["quora.com"]},
    {"name":"Duolingo","difficulty":"easy","url":"https://www.duolingo.com/settings/account","domains":["duolingo.com"]},
    {"name":"Coursera","difficulty":"medium","url":"https://www.coursera.org/account/privacy","domains":["coursera.org"]},
    {"name":"Udemy","difficulty":"hard","url":"https://www.udemy.com/user/delete-account/","domains":["udemy.com"]},
    {"name":"Zoom","difficulty":"easy","url":"https://zoom.us/profile/advanced","domains":["zoom.us"]},
    {"name":"Skype","difficulty":"hard","url":"https://go.skype.com/myaccount","domains":["skype.com"]},
    {"name":"WhatsApp","difficulty":"easy","url":"https://faq.whatsapp.com/general/account-and-profile/how-to-delete-your-account","domains":["whatsapp.com"]},
    {"name":"Telegram","difficulty":"easy","url":"https://telegram.org/deactivate","domains":["telegram.org","t.me"]},
    {"name":"Signal","difficulty":"easy","url":"https://support.signal.org/hc/en-us/articles/360007061192","domains":["signal.org"]},
    {"name":"Etsy","difficulty":"hard","url":"https://help.etsy.com/hc/en-us/articles/115015667188","domains":["etsy.com"]},
    {"name":"Venmo","difficulty":"hard","url":"https://help.venmo.com/hc/en-us/articles/209690668","domains":["venmo.com"]},
    {"name":"Cash App","difficulty":"medium","url":"https://cash.app/help/us/en-us/5113","domains":["cash.app"]},
    {"name":"Coinbase","difficulty":"hard","url":"https://help.coinbase.com/en/coinbase/privacy-and-security/privacy/how-do-i-delete-my-account","domains":["coinbase.com"]},
    {"name":"Robinhood","difficulty":"medium","url":"https://robinhood.com/us/en/support/articles/360001226706/","domains":["robinhood.com"]},
    {"name":"Canva","difficulty":"easy","url":"https://www.canva.com/settings/account","domains":["canva.com"]},
    {"name":"Figma","difficulty":"easy","url":"https://www.figma.com/settings","domains":["figma.com"]},
    {"name":"Notion","difficulty":"easy","url":"https://www.notion.so/my-account","domains":["notion.so"]},
    {"name":"Trello","difficulty":"easy","url":"https://trello.com/deactivated-account","domains":["trello.com"]},
    {"name":"Asana","difficulty":"hard","url":"https://asana.com/guide/help/fundamentals/account-management","domains":["asana.com"]},
    {"name":"Eventbrite","difficulty":"easy","url":"https://www.eventbrite.com/account-settings/close","domains":["eventbrite.com"]},
    {"name":"Meetup","difficulty":"easy","url":"https://www.meetup.com/account/delete/","domains":["meetup.com"]},
    {"name":"Goodreads","difficulty":"easy","url":"https://www.goodreads.com/user/destroy","domains":["goodreads.com"]},
    {"name":"Last.fm","difficulty":"easy","url":"https://www.last.fm/settings/account/deactivate","domains":["last.fm"]},
    {"name":"SoundCloud","difficulty":"medium","url":"https://soundcloud.com/settings/account","domains":["soundcloud.com"]},
    {"name":"Bandcamp","difficulty":"medium","url":"https://bandcamp.com/settings","domains":["bandcamp.com"]},
    {"name":"Shazam","difficulty":"easy","url":"https://www.shazam.com/myshazam","domains":["shazam.com"]},
    {"name":"PlayStation Network","difficulty":"hard","url":"https://www.playstation.com/en-us/my-playstation/close-account/","domains":["playstation.com","psn.com"]},
    {"name":"Xbox","difficulty":"hard","url":"https://account.microsoft.com/account","domains":["xbox.com","xboxlive.com"]},
    {"name":"Steam","difficulty":"hard","url":"https://help.steampowered.com/en/faqs/view/69E2-B965-C8D5-E77F","domains":["steampowered.com","steamcommunity.com"]},
    {"name":"Epic Games","difficulty":"medium","url":"https://www.epicgames.com/help/en-US/epic-accounts-c74/delete-your-epic-games-account-a8876","domains":["epicgames.com"]},
    {"name":"Roblox","difficulty":"impossible","url":"https://en.help.roblox.com/hc/en-us/articles/203314580","domains":["roblox.com"]},
    {"name":"Minecraft","difficulty":"impossible","url":"https://help.minecraft.net/hc/en-us/requests/new","domains":["minecraft.net","mojang.com"]},
    {"name":"Riot Games","difficulty":"medium","url":"https://support-leagueoflegends.riotgames.com/hc/en-us/requests/new","domains":["riotgames.com","leagueoflegends.com","valorant.com"]},
    {"name":"Blizzard","difficulty":"hard","url":"https://us.battle.net/support/en/article/76459","domains":["battle.net","blizzard.com"]},
    {"name":"Origin / EA","difficulty":"hard","url":"https://help.ea.com/en/help/account/delete-your-ea-account/","domains":["ea.com","origin.com"]},
    {"name":"Hulu","difficulty":"hard","url":"https://help.hulu.com/s/article/cancel-subscription","domains":["hulu.com"]},
    {"name":"Disney+","difficulty":"hard","url":"https://help.disneyplus.com/csp","domains":["disneyplus.com"]},
    {"name":"HBO Max","difficulty":"hard","url":"https://help.max.com/us/Answer/Detail/000002006","domains":["hbomax.com","max.com"]},
    {"name":"Paramount+","difficulty":"hard","url":"https://help.paramountplus.com/s/article/PD-How-do-I-cancel-my-Paramount-subscription","domains":["paramountplus.com","cbs.com"]},
    {"name":"Apple ID","difficulty":"hard","url":"https://privacy.apple.com/","domains":["apple.com","appleid.apple.com"]},
    {"name":"Shopify","difficulty":"medium","url":"https://help.shopify.com/en/manual/your-account/close-your-store","domains":["shopify.com","myshopify.com"]},
    {"name":"Wix","difficulty":"medium","url":"https://support.wix.com/en/article/deactivating-and-closing-your-wix-account","domains":["wix.com"]},
    {"name":"Squarespace","difficulty":"hard","url":"https://support.squarespace.com/hc/en-us/articles/205812028","domains":["squarespace.com"]},
    {"name":"WordPress.com","difficulty":"easy","url":"https://wordpress.com/me/account/close","domains":["wordpress.com"]},
    {"name":"Blogger","difficulty":"hard","url":"https://support.google.com/blogger/answer/41387","domains":["blogger.com","blogspot.com"]},
    {"name":"Patreon","difficulty":"medium","url":"https://support.patreon.com/hc/en-us/articles/212052266","domains":["patreon.com"]},
    {"name":"OnlyFans","difficulty":"medium","url":"https://onlyfans.com/my/settings/account","domains":["onlyfans.com"]},
    {"name":"Kickstarter","difficulty":"hard","url":"https://help.kickstarter.com/hc/en-us/articles/115005028294","domains":["kickstarter.com"]},
    {"name":"GoFundMe","difficulty":"hard","url":"https://www.gofundme.com/settings","domains":["gofundme.com"]},
    {"name":"Mailchimp","difficulty":"easy","url":"https://mailchimp.com/help/delete-account/","domains":["mailchimp.com"]},
    {"name":"HubSpot","difficulty":"hard","url":"https://knowledge.hubspot.com/account/delete-a-hubspot-account","domains":["hubspot.com"]},
    {"name":"Salesforce","difficulty":"impossible","url":"https://help.salesforce.com/","domains":["salesforce.com"]},
    {"name":"Fiverr","difficulty":"easy","url":"https://www.fiverr.com/support/articles/360010451117","domains":["fiverr.com"]},
    {"name":"Upwork","difficulty":"medium","url":"https://support.upwork.com/hc/en-us/articles/211062568","domains":["upwork.com"]},
    {"name":"Freelancer","difficulty":"hard","url":"https://www.freelancer.com/support","domains":["freelancer.com"]},
    {"name":"TaskRabbit","difficulty":"hard","url":"https://support.taskrabbit.com/hc/en-us","domains":["taskrabbit.com"]},
    {"name":"Grubhub","difficulty":"hard","url":"https://www.grubhub.com/help/contact-us","domains":["grubhub.com"]},
    {"name":"DoorDash","difficulty":"hard","url":"https://help.doordash.com/consumers/s/article/how-do-i-delete-my-account","domains":["doordash.com"]},
    {"name":"Instacart","difficulty":"hard","url":"https://www.instacart.com/privacy","domains":["instacart.com"]},
    {"name":"Yelp","difficulty":"medium","url":"https://www.yelp.com/profile_privacy","domains":["yelp.com"]},
    {"name":"TripAdvisor","difficulty":"medium","url":"https://www.tripadvisor.com/MemberProfile","domains":["tripadvisor.com"]},
    {"name":"Booking.com","difficulty":"hard","url":"https://account.booking.com/mysettings","domains":["booking.com"]},
    {"name":"Expedia","difficulty":"hard","url":"https://www.expedia.com/service/","domains":["expedia.com"]},
    {"name":"Hotels.com","difficulty":"hard","url":"https://www.hotels.com/service/","domains":["hotels.com"]},
    {"name":"Ticketmaster","difficulty":"impossible","url":"","domains":["ticketmaster.com"]},
    {"name":"StubHub","difficulty":"hard","url":"https://www.stubhub.com/helpdesk/contact_us.html","domains":["stubhub.com"]},
    {"name":"Nike","difficulty":"hard","url":"https://www.nike.com/help/a/account-info","domains":["nike.com"]},
    {"name":"ASOS","difficulty":"hard","url":"https://www.asos.com/us/customer-care/your-account/","domains":["asos.com"]},
    {"name":"Zalando","difficulty":"medium","url":"https://www.zalando.co.uk/myaccount/settings/","domains":["zalando.com","zalando.co.uk"]},
    {"name":"H&M","difficulty":"hard","url":"https://www.hm.com/us/customer-service","domains":["hm.com"]},
    {"name":"ZARA","difficulty":"hard","url":"https://www.zara.com/us/en/contact.html","domains":["zara.com"]},
    {"name":"Next","difficulty":"hard","url":"https://www.next.co.uk/help/contactus/","domains":["next.co.uk"]},
    {"name":"Deliveroo","difficulty":"hard","url":"https://deliveroo.co.uk/privacy","domains":["deliveroo.co.uk","deliveroo.com"]},
    {"name":"Just Eat","difficulty":"hard","url":"https://www.just-eat.co.uk/account","domains":["just-eat.co.uk","just-eat.com"]},
    {"name":"Uber Eats","difficulty":"hard","url":"https://help.uber.com/ubereats/","domains":["ubereats.com"]},
    {"name":"Revolut","difficulty":"easy","url":"https://www.revolut.com/legal/privacy","domains":["revolut.com"]},
    {"name":"Monzo","difficulty":"medium","url":"https://monzo.com/blog/close-your-account","domains":["monzo.com"]},
    {"name":"Wise","difficulty":"hard","url":"https://wise.com/help/articles/2932225","domains":["wise.com","transferwise.com"]},
    {"name":"Klarna","difficulty":"hard","url":"https://www.klarna.com/us/customer-service/","domains":["klarna.com"]},
    {"name":"Afterpay","difficulty":"hard","url":"https://help.afterpay.com/hc/en-us/requests/new","domains":["afterpay.com"]},
    {"name":"Strava","difficulty":"easy","url":"https://www.strava.com/settings/profile","domains":["strava.com"]},
    {"name":"Fitbit","difficulty":"hard","url":"https://www.fitbit.com/settings/account","domains":["fitbit.com"]},
    {"name":"MyFitnessPal","difficulty":"easy","url":"https://www.myfitnesspal.com/user/account_delete","domains":["myfitnesspal.com"]},
    {"name":"Headspace","difficulty":"medium","url":"https://www.headspace.com/privacy","domains":["headspace.com"]},
    {"name":"Calm","difficulty":"medium","url":"https://www.calm.com/app/settings","domains":["calm.com"]},
    {"name":"Peloton","difficulty":"hard","url":"https://support.onepeloton.com/hc/en-us/requests/new","domains":["onepeloton.com"]},
    {"name":"Freeletics","difficulty":"medium","url":"https://www.freeletics.com/en/corporate/privacy/","domains":["freeletics.com"]},
    {"name":"Imgur","difficulty":"easy","url":"https://imgur.com/account/settings/delete","domains":["imgur.com"]},
    {"name":"DeviantArt","difficulty":"easy","url":"https://www.deviantart.com/settings/account","domains":["deviantart.com"]},
    {"name":"Behance","difficulty":"impossible","url":"","domains":["behance.net"]},
    {"name":"500px","difficulty":"medium","url":"https://500px.com/settings/account","domains":["500px.com"]},
    {"name":"Flickr","difficulty":"easy","url":"https://www.flickr.com/account/delete","domains":["flickr.com"]},
    {"name":"SmugMug","difficulty":"medium","url":"https://www.smugmug.com/help/delete-account","domains":["smugmug.com"]},
]

def _build_jdm_index(services: list) -> dict:
    index = {}
    for svc in services:
        name    = svc.get("name","").strip()
        domains = svc.get("domains",[])
        if isinstance(domains, str):
            domains = [domains]
        entry = {
            "name":       name,
            "difficulty": svc.get("difficulty","unknown").lower(),
            "url":        svc.get("url",""),
            "notes":      svc.get("notes",""),
            "email":      svc.get("email",""),
            "domains":    domains,
        }
        index[name.lower()] = entry
        for d in domains:
            index[d.lower().lstrip("www.")] = entry
    return index

def fetch_jdm(force_refresh=False) -> dict:
    """Fetch JDM data with corrected URLs and offline fallback."""
    if JDM_CACHE.exists() and not force_refresh:
        age = time.time() - JDM_CACHE.stat().st_mtime
        if age < 86400 * 7:
            try:
                data = json.loads(JDM_CACHE.read_text())
                if data:
                    return data
            except Exception:
                pass

    APP_DIR.mkdir(parents=True, exist_ok=True)
    raw = None

    for url in JDM_URLS:
        try:
            console.print(f"[dim]  Trying {url[:60]}…[/]")
            r = requests.get(url, timeout=12,
                             headers={"User-Agent": "account-nuker/1.2"})
            r.raise_for_status()
            raw = r.json()
            log.info("JDM fetched from %s (%d services)", url, len(raw))
            break
        except Exception as e:
            log.warning("JDM fetch failed [%s]: %s", url, e)

    if raw:
        index = _build_jdm_index(raw)
        JDM_CACHE.write_text(json.dumps(index, indent=2))
        console.print(f"[green]✓ JDM data loaded: {len(raw)} services[/]")
        return index
    else:
        # Use offline built-in fallback
        console.print(
            "[yellow]⚠ Could not reach JustDeleteMe — using built-in database "
            f"({len(JDM_FALLBACK_SERVICES)} popular services)[/]"
        )
        log.warning("JDM unreachable, using built-in fallback (%d services)",
                    len(JDM_FALLBACK_SERVICES))
        index = _build_jdm_index(JDM_FALLBACK_SERVICES)
        # Cache the fallback too
        JDM_CACHE.write_text(json.dumps(index, indent=2))
        return index


# ── FIX 3: Gmail All Mail search + broader IMAP scanning ─────────────────────
def _decode_header_str(hdr) -> str:
    parts = decode_header(hdr or "")
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return " ".join(out)

def extract_domain_from_sender(sender: str) -> Optional[str]:
    match = re.search(r"@([\w.\-]+)", sender)
    if not match:
        return None
    domain = match.group(1).lower()
    for prefix in ("mail.", "email.", "noreply.", "no-reply.", "notifications.",
                   "info.", "support.", "hello.", "mailer.", "post.", "news.",
                   "newsletter.", "updates.", "reply.", "bounce.", "send."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain

def _get_mailboxes_to_search(mail: imaplib.IMAP4_SSL, email_addr: str) -> list:
    """
    Return list of mailbox names to search.
    For Gmail: must include [Gmail]/All Mail to get everything.
    """
    domain = email_addr.split("@")[-1].lower()
    is_gmail = domain in ("gmail.com", "googlemail.com")

    mailboxes = ["INBOX"]

    if is_gmail:
        # Gmail labels to search — All Mail has every message
        gmail_boxes = [
            '"[Gmail]/All Mail"',
            '"[Google Mail]/All Mail"',  # some locales use this
        ]
        try:
            _, list_data = mail.list()
            box_names = []
            for item in (list_data or []):
                if isinstance(item, bytes):
                    # Parse mailbox name from LIST response
                    m = re.search(r'"([^"]+)"\s*$|(\S+)\s*$', item.decode("utf-8", errors="replace"))
                    if m:
                        box_names.append((m.group(1) or m.group(2)).strip('"'))

            # Try common All Mail names
            for candidate in gmail_boxes:
                clean = candidate.strip('"')
                if any(clean.lower() == b.lower() for b in box_names):
                    mailboxes = [candidate]  # Replace INBOX with All Mail
                    log.info("Using Gmail All Mail: %s", candidate)
                    return mailboxes
            # Fallback: add both common names
            mailboxes += gmail_boxes
        except Exception as e:
            log.warning("Could not list Gmail mailboxes: %s", e)
            mailboxes += ['"[Gmail]/All Mail"']

    return mailboxes

def scan_email(email_addr: str, password: str, dry_run=False,
               progress_callback=None) -> set:
    if dry_run:
        return {"example.com","spotify.com","netflix.com","amazon.co.uk",
                "github.com","discord.com","reddit.com"}

    domain = email_addr.split("@")[-1].lower()
    host, port = IMAP_HOSTS.get(domain, (f"imap.{domain}", 993))

    domains: set = set()
    try:
        console.print(f"[cyan]Connecting to {host}:{port}…[/]")
        mail = imaplib.IMAP4_SSL(host, port)
        mail.login(email_addr, password)
        log.info("IMAP login OK: %s @ %s", email_addr, host)
    except imaplib.IMAP4.error as e:
        console.print(f"[red]✗ Login failed: {e}[/]")
        console.print(_app_password_hint(email_addr))
        log.error("IMAP login failed: %s", e)
        return domains

    # ── FIX: Search Gmail All Mail, not just INBOX ────────────────────────
    mailboxes = _get_mailboxes_to_search(mail, email_addr)
    since_date = (datetime.now() - timedelta(days=3650)).strftime("%d-%b-%Y")
    found_ids: set = set()

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console) as prog:
        task = prog.add_task("Searching mailboxes…", total=len(mailboxes) + 1)

        for mbox in mailboxes:
            try:
                status, _ = mail.select(mbox, readonly=True)
                if status != "OK":
                    log.warning("Could not select mailbox %s", mbox)
                    prog.advance(task)
                    continue
            except Exception as e:
                log.warning("Mailbox select error %s: %s", mbox, e)
                prog.advance(task)
                continue

            # Strategy 1: Gmail X-GM-RAW search (best for Gmail, finds ALL registration emails)
            is_gmail_mbox = "gmail" in mbox.lower() or "google" in mbox.lower() or "all mail" in mbox.lower()
            if is_gmail_mbox:
                try:
                    query = ('(subject:welcome OR subject:confirm OR subject:verify ' +
                             'OR subject:"account created" OR subject:registration ' +
                             'OR subject:"signed up" OR subject:activate ' +
                             'OR subject:"thanks for joining" OR subject:"verify your email")')
                    _, data = mail.search(None, f'X-GM-RAW "{query}"')
                    ids = data[0].split() if data and data[0] else []
                    found_ids.update(ids)
                    log.info("X-GM-RAW search on %s: %d results", mbox, len(ids))
                    if ids:
                        prog.advance(task)
                        continue
                except Exception as e:
                    log.debug("X-GM-RAW search failed (expected on non-Gmail): %s", e)

            # Strategy 2: Search ALL messages since date — fetch From: headers to find registrations
            # This is the most reliable fallback: get all message IDs, sample senders
            try:
                _, data = mail.search(None, f'SINCE "{since_date}"')
                all_ids = data[0].split() if data and data[0] else []
                log.info("All messages since %s in %s: %d", since_date, mbox, len(all_ids))
                # Add all to found_ids (we'll filter by sender pattern when fetching)
                found_ids.update(all_ids)
            except Exception as e:
                log.warning("ALL search in %s: %s", mbox, e)

                # Strategy 3: Subject keyword search as final fallback
                for kw in SEARCH_SUBJECTS:
                    try:
                        _, data = mail.search(None, f'(SINCE "{since_date}" SUBJECT "{kw}")')
                        ids = data[0].split() if data and data[0] else []
                        found_ids.update(ids)
                    except Exception as ke:
                        log.debug("Keyword search '%s': %s", kw, ke)

            prog.advance(task)
        prog.advance(task)  # final tick

    log.info("Found %d candidate email IDs", len(found_ids))
    found_list = list(found_ids)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  console=console) as prog:
        task = prog.add_task(f"Reading {len(found_list)} emails…",
                             total=max(len(found_list), 1))
        # Re-select last mailbox (All Mail preferred)
        try:
            mail.select(mailboxes[-1], readonly=True)
        except Exception:
            mail.select("INBOX", readonly=True)

        for eid in found_list:
            try:
                _, msg_data = mail.fetch(eid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                if not msg_data or not msg_data[0]:
                    prog.advance(task); continue
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)
                sender = _decode_header_str(msg.get("From", ""))
                dom = extract_domain_from_sender(sender)
                if dom and "." in dom and len(dom) > 3:
                    domains.add(dom)
            except Exception as e:
                log.debug("Email fetch error id=%s: %s", eid, e)
            prog.advance(task)
            if progress_callback:
                progress_callback(len(domains))

    mail.logout()
    log.info("Email scan: %d domains found", len(domains))
    return domains


# ── FIX 4: Expanded browser paths for Kali Linux ─────────────────────────────
def _copy_db(src: Path) -> Optional[Path]:
    tmp = Path(tempfile.mktemp(suffix=".sqlite", dir="/tmp"))
    try:
        shutil.copy2(src, tmp)
        return tmp
    except Exception as e:
        log.warning("Could not copy %s: %s", src, e)
        return None

def scan_browser_history() -> set:
    domains: set = set()
    profiles = _get_browser_profiles()

    for browser, paths in profiles.items():
        path_list = list(paths) if not isinstance(paths, list) else paths
        for db_path in path_list:
            if not _safe_exists(Path(db_path)):
                continue
            tmp = _copy_db(Path(db_path))
            if not tmp:
                continue
            try:
                conn = sqlite3.connect(str(tmp))
                if browser == "Firefox":
                    cur = conn.execute(
                        "SELECT url FROM moz_places WHERE visit_count > 0 LIMIT 100000"
                    )
                else:
                    cur = conn.execute(
                        "SELECT url FROM urls ORDER BY visit_count DESC LIMIT 100000"
                    )
                for (url,) in cur.fetchall():
                    try:
                        host = re.sub(r"^www\.", "",
                                      urlparse(url).hostname or "").lower()
                        if host and "." in host and len(host) > 3:
                            domains.add(host)
                    except Exception:
                        pass
                conn.close()
                console.print(f"[green]✓ {browser} history: {db_path}[/]")
                log.info("Browser %s scanned: %s — %d domains so far",
                         browser, db_path, len(domains))
            except Exception as e:
                log.warning("Browser DB error (%s @ %s): %s", browser, db_path, e)
            finally:
                try:
                    tmp.unlink()
                except Exception:
                    pass

    log.info("Browser scan complete: %d domains", len(domains))
    return domains


# ── JDM matching ───────────────────────────────────────────────────────────────
def match_to_jdm(domains: set, jdm: dict) -> list:
    DIFF_ORDER = {"easy":0,"medium":1,"hard":2,"impossible":3,"unknown":4}
    results, seen_names = [], set()

    for domain in sorted(domains):
        clean = domain.lower().lstrip("www.")
        entry = jdm.get(clean)
        if not entry:
            parts = clean.split(".")
            if len(parts) > 2:
                entry = jdm.get(".".join(parts[-2:]))
        if not entry:
            for key, val in jdm.items():
                if clean in key or key in clean:
                    entry = val
                    break

        if entry:
            name = entry["name"]
            if name in seen_names:
                continue
            seen_names.add(name)
            results.append({
                "domain":     domain,
                "name":       name,
                "difficulty": entry["difficulty"],
                "url":        entry["url"],
                "notes":      entry["notes"],
                "email":      entry["email"],
            })
        else:
            results.append({
                "domain":     domain,
                "name":       domain,
                "difficulty": "unknown",
                "url":        f"https://{domain}",
                "notes":      "Not in database",
                "email":      "",
            })

    results.sort(key=lambda x: (DIFF_ORDER.get(x["difficulty"],4), x["name"].lower()))
    return results


# ── GDPR template ──────────────────────────────────────────────────────────────
def generate_gdpr_email(service_name, service_email, user_email, domain):
    return f"""Subject: Right to Erasure Request (GDPR Art. 17 / CCPA) — {user_email}

To Whom It May Concern,

I am writing to formally request the permanent deletion of all personal data
associated with my account under the following regulations:

  • EU General Data Protection Regulation (GDPR) — Article 17, Right to Erasure
  • California Consumer Privacy Act (CCPA) — Right to Delete

Account details:
  Service:  {service_name} ({domain})
  Email:    {user_email}

Please confirm in writing:
  1. All personal data has been deleted from your systems and backups.
  2. No further processing of my data will occur.
  3. Any third parties who received my data have been notified.

I expect a response within 30 days as required by GDPR Art. 12(3).

Regards,
{user_email}
"""


# ── FIX 2: Better app password hints ──────────────────────────────────────────
def _app_password_hint(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].lower()
    if domain in ("gmail.com", "googlemail.com"):
        return (
            "\n[bold yellow]⚠  Gmail requires an App Password — NOT your regular password.[/]\n"
            "[yellow]Steps:[/]\n"
            "  1. Go to: [link=https://myaccount.google.com/apppasswords]"
            "https://myaccount.google.com/apppasswords[/link]\n"
            "  2. Sign in → Select app: Mail → Select device: Other\n"
            "  3. Copy the 16-character password shown (spaces are optional)\n"
            "  4. Re-run account-nuker and use THAT password\n"
            "[dim]Note: 2-Step Verification must be enabled on your Google account first.[/]\n"
        )
    elif domain in ("outlook.com","hotmail.com","live.com"):
        return (
            "[yellow]Outlook: Enable IMAP at\n"
            "  https://outlook.live.com/mail/options/mail/popimap\n"
            "Then use your normal Outlook password.[/]"
        )
    elif domain in ("yahoo.com","ymail.com"):
        return (
            "[yellow]Yahoo: Generate App Password at\n"
            "  https://login.yahoo.com/account/security → Manage App Passwords[/]"
        )
    return "[yellow]Use an app-specific password if 2FA is enabled.[/]"


# ── CSV export ─────────────────────────────────────────────────────────────────
def export_csv(accounts: list):
    with open(REPORT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["name","domain","difficulty","url","notes","email"])
        writer.writeheader()
        writer.writerows(accounts)
    console.print(f"[green]✓ CSV: {REPORT_CSV}[/]")
    log.info("CSV exported: %s (%d rows)", REPORT_CSV, len(accounts))


# ── CLI ────────────────────────────────────────────────────────────────────────
def display_accounts_table(accounts):
    table = Table(title="[bold cyan]Discovered Accounts[/]",
                  show_lines=True, expand=True,
                  header_style="bold white on dark_blue")
    table.add_column("#",          style="dim",   width=4)
    table.add_column("Service",    style="bold",  min_width=18)
    table.add_column("Domain",     style="cyan",  min_width=18)
    table.add_column("Difficulty", justify="center", min_width=12)
    table.add_column("Auto?",      justify="center", width=6)
    table.add_column("URL",        min_width=30)

    for i, acc in enumerate(accounts, 1):
        diff  = acc["difficulty"]
        color = DIFFICULTY_COLORS.get(diff, "white")
        diff_cell = Text(diff.upper(), style=f"bold {color}")
        auto_badge = Text("✓", style="green bold") if diff in ("easy","medium") else Text("—", style="dim")
        url = acc.get("url","") or f"https://{acc['domain']}"
        table.add_row(str(i), acc["name"], acc["domain"], diff_cell, auto_badge,
                      f"[link={url}]{url[:50]}[/link]" if url else "—")

    console.print(table)
    auto_count = sum(1 for a in accounts if a["difficulty"] in ("easy","medium"))
    console.print(f"[dim]  {auto_count}/{len(accounts)} automatable[/]")


def prompt_credentials():
    stored = load_creds()
    if stored:
        email_addr, password = stored
        console.print(f"[green]✓ Saved: [bold]{email_addr}[/]")
        if not Confirm.ask("Use these credentials?", default=True):
            delete_creds(); stored = None
    if not stored:
        email_addr = Prompt.ask("[cyan]Email address[/]")
        console.print(_app_password_hint(email_addr))
        password = getpass.getpass("App/account password (hidden): ")
        if Confirm.ask("Save credentials locally?", default=True):
            save_creds(email_addr, password)
    return email_addr, password


def interactive_menu(accounts, user_email, user_password, dry_run=False):
    while True:
        console.rule("[cyan]Menu[/]")
        console.print(
            " [bold cyan]o[/]  Open in browser\n"
            " [bold cyan]a[/]  Automate with Playwright\n"
            " [bold cyan]g[/]  Generate GDPR email\n"
            " [bold cyan]e[/]  Export CSV\n"
            " [bold cyan]r[/]  Refresh table\n"
            " [bold cyan]q[/]  Quit\n"
        )
        choice = Prompt.ask("[bold cyan]>[/]", default="q").strip().lower()
        if choice == "q": break
        elif choice == "r": display_accounts_table(accounts)
        elif choice == "e": export_csv(accounts)
        elif choice == "o":
            idx_str = Prompt.ask("Account # (or 'all' for easy/medium)")
            if idx_str.lower() == "all":
                opened = 0
                for acc in accounts:
                    if acc["difficulty"] in ("easy","medium") and acc["url"]:
                        subprocess.Popen(["xdg-open", acc["url"]],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        opened += 1; time.sleep(0.3)
                console.print(f"[green]Opened {opened} tabs.[/]")
            else:
                try:
                    acc = accounts[int(idx_str)-1]
                    subprocess.Popen(["xdg-open", acc["url"]],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (ValueError, IndexError):
                    console.print("[red]Invalid #[/]")
        elif choice == "a":
            try:
                sys.path.insert(0, str(Path(__file__).parent))
                from browser_automation import run_automation
                targets = [a for a in accounts if a["difficulty"] in ("easy","medium")]
                if not targets:
                    console.print("[yellow]No easy/medium accounts.[/]")
                    continue
                results = run_automation(targets, user_email, user_password,
                                         APP_DIR, dry_run=dry_run)
                for r in results:
                    console.print(f"  {r['service']}: [bold]{r['status']}[/]")
            except Exception as e:
                console.print(f"[red]Automation error: {e}[/]")
        elif choice == "g":
            idx_str = Prompt.ask("Account #")
            try:
                acc = accounts[int(idx_str)-1]
                mailto = acc.get("email") or Prompt.ask("Privacy email")
                template = generate_gdpr_email(acc["name"], mailto, user_email, acc["domain"])
                out = APP_DIR / f"gdpr_{acc['domain']}.txt"
                out.write_text(template)
                console.print(Panel(template, title=f"GDPR — {acc['name']}", border_style="yellow"))
                console.print(f"[green]✓ Saved: {out}[/]")
            except (ValueError, IndexError):
                console.print("[red]Invalid #[/]")


@click.command()
@click.option("--dry-run",         is_flag=True)
@click.option("--email-only",      is_flag=True)
@click.option("--browser-only",    is_flag=True)
@click.option("--refresh-jdm",     is_flag=True)
@click.option("--delete-creds",    is_flag=True)
@click.option("--export-csv",      is_flag=True)
@click.option("--no-interactive",  is_flag=True)
@click.option("--automate",        is_flag=True)
@click.option("--headless",        is_flag=True)
@click.option("--filter","difficulty_filter",
              type=click.Choice(["easy","medium","hard","impossible","unknown"],
                                case_sensitive=False), default=None)
@click.version_option(VERSION, prog_name="account-nuker")
def main(dry_run, email_only, browser_only, refresh_jdm,
         delete_creds, export_csv, no_interactive,
         automate, headless, difficulty_filter):
    """account-nuker: Discover and delete your online accounts."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    log.info("=== account-nuker v%s started ===", VERSION)

    if delete_creds:
        globals()["delete_creds"]()
        return

    console.print(Panel.fit(
        f"[bold cyan]account-nuker[/] [dim]v{VERSION}[/]\n"
        "[dim]Privacy-first · Local-only[/]", border_style="cyan"))

    user_email = user_password = ""
    if not browser_only:
        user_email, user_password = prompt_credentials()

    jdm = fetch_jdm(force_refresh=refresh_jdm)
    console.print(f"[dim]JDM: {len(jdm):,} entries[/]\n")

    all_domains: set = set()
    if not browser_only:
        console.rule("[cyan]📧 Email Scan[/]")
        ed = scan_email(user_email, user_password, dry_run=dry_run)
        console.print(f"[green]✓ Email: {len(ed)} domains[/]")
        all_domains |= ed

    if not email_only:
        console.rule("[cyan]🌐 Browser Scan[/]")
        bd = scan_browser_history() if not dry_run else {"spotify.com","netflix.com","github.com"}
        console.print(f"[green]✓ Browser: {len(bd)} domains[/]")
        all_domains |= bd

    all_domains -= NOISE_DOMAINS
    console.print(f"\n[bold green]Total: {len(all_domains)} unique domains[/]\n")

    if not all_domains:
        console.print("[yellow]No domains found.[/]")
        return

    accounts = match_to_jdm(all_domains, jdm)
    if difficulty_filter:
        accounts = [a for a in accounts if a["difficulty"] == difficulty_filter]

    console.print(f"[bold]Found [cyan]{len(accounts)}[/cyan] accounts.[/]\n")
    display_accounts_table(accounts)

    if export_csv:
        globals()["export_csv"](accounts)

    if automate:
        targets = [a for a in accounts if a["difficulty"] in ("easy","medium")]
        if targets:
            try:
                sys.path.insert(0, str(Path(__file__).parent))
                from browser_automation import run_automation
                results = run_automation(targets, user_email, user_password,
                                          APP_DIR, dry_run=dry_run, headless=headless)
                for r in results:
                    console.print(f"  {r['service']}: {r['status']}")
            except Exception as e:
                console.print(f"[red]Automation error: {e}[/]")

    if not no_interactive:
        interactive_menu(accounts, user_email, user_password, dry_run=dry_run)

    console.print(f"\n[dim]Log → {LOG_FILE}[/]")
    log.info("=== account-nuker finished ===")


if __name__ == "__main__":
    main(standalone_mode=True)
