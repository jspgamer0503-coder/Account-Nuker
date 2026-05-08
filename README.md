# account-nuker

> **Find and delete your online accounts — privately, locally, zero cloud.**

A privacy-first CLI + web dashboard that scans your own email inbox to discover accounts you've registered across the web, then walks you through deleting them using [JustDeleteMe](https://justdeleteme.xyz). All processing happens on your machine. Your credentials never leave it.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS-lightgrey?logo=linux)
![License](https://img.shields.io/badge/License-MIT-green)
![Version](https://img.shields.io/badge/Version-1.2.0-informational)

---

## Features

- 📧 **IMAP inbox scan** — connects to Gmail, Outlook, Yahoo, or any IMAP server to find registration and welcome emails
- 🔍 **JustDeleteMe lookup** — cross-references discovered services against the JDM database to find direct deletion URLs
- 🌐 **Browser automation** — headful Playwright session handles login → settings → delete flows with human-like timing
- ⏸️ **CAPTCHA pause-and-resume** — detects reCAPTCHA, hCaptcha, and Cloudflare Turnstile, pauses for you to solve manually, then resumes
- 🖥️ **Web dashboard** — local Flask UI at `http://localhost:7734` for a point-and-click experience
- 📊 **CSV report** — exports findings to `~/account-nuker-report.csv`
- 🔐 **Local-only** — credentials stored in `~/.account-nuker/creds.json`, never transmitted

---

## Interfaces

| Interface | File | How to run |
|---|---|---|
| CLI (Rich TUI) | `app.py` | `python3 app.py` |
| Web dashboard | `gui_app.py` | `python3 gui_app.py` → opens `http://localhost:7734` |

---

## Installation

### Quick install (Linux/macOS)

```bash
curl -sL https://raw.githubusercontent.com/YOUR_USERNAME/account-nuker/main/install.sh | bash
```

### Manual install

```bash
git clone https://github.com/YOUR_USERNAME/account-nuker.git
cd account-nuker
pip install -r requirements.txt
playwright install chromium --with-deps
```

---

## Gmail Setup (App Password)

Gmail requires an **App Password** — it does not accept your regular password over IMAP.

1. Enable 2-Step Verification on your Google account
2. Go to **Google Account → Security → App Passwords**
3. Generate a new app password (select "Mail" + "Linux device")
4. Use the generated 16-character code when account-nuker asks for your password

> ⚠️ **Never commit your App Password to Git.** See [Security](#security) below.

---

## Usage

### CLI

```bash
python3 app.py
```

You will be prompted for your email address and IMAP password (App Password for Gmail). The tool will:

1. Connect to your inbox via IMAP
2. Scan for registration/welcome emails
3. Match found services against the JustDeleteMe database
4. Show a ranked table with deletion difficulty (Easy / Medium / Hard / Impossible)
5. Optionally launch browser automation to delete accounts for you

### Web Dashboard

```bash
python3 gui_app.py
```

Opens `http://localhost:7734` in your browser. Same workflow as the CLI but point-and-click.

---

## Supported Email Providers

| Provider | IMAP Host |
|---|---|
| Gmail / Google Workspace | `imap.gmail.com:993` |
| Outlook / Hotmail / Live | `imap-mail.outlook.com:993` |
| Yahoo Mail | `imap.mail.yahoo.com:993` |
| Any other | Enter your provider's IMAP host manually |

---

## Security

### Do not commit credentials

The file `account password` (or any file containing your IMAP/App Password) must **never** be committed to Git. This repo's `.gitignore` covers common patterns, but always double-check with:

```bash
git status
git diff --cached
```

If you accidentally commit a credential, **revoke it immediately**:
- Gmail: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) → delete the app password
- Then rotate: generate a new one and update your local config

### Credential storage

Saved credentials are stored in `~/.account-nuker/creds.json` (mode `600`). They are never sent anywhere other than your email provider's IMAP server.

### Browser sessions

Playwright sessions are persisted to `~/.account-nuker/sessions/<domain>.json` so you don't have to re-login each run. These are local-only.

---

## Project Structure

```
account-nuker/
├── app.py                  # CLI entry point (Rich TUI)
├── gui_app.py              # Web dashboard (Flask, localhost:7734)
├── browser_automation.py   # Playwright automation engine
├── install.sh              # One-line installer script
├── requirements.txt        # Python dependencies
└── README.md
```

---

## Output Files

| File | Contents |
|---|---|
| `~/account-nuker-report.csv` | Full scan results — service, URL, JDM difficulty, status |
| `~/account-nuker.log` | Per-session operation log |
| `~/account-nuker-automation.json` | Browser automation results |
| `~/.account-nuker/creds.json` | Saved IMAP credentials (local only, mode 600) |
| `~/.account-nuker/jdm.json` | Cached JustDeleteMe service list |

---

## Troubleshooting

### IMAP authentication failed (Gmail)
You must use an App Password, not your Google account password. See [Gmail Setup](#gmail-setup-app-password).

### `playwright install` fails
Run manually:
```bash
python3 -m playwright install chromium --with-deps
```

### No accounts found
- Make sure IMAP access is enabled in your email settings
- For Gmail: **Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP**
- Try scanning "All Mail" instead of just Inbox

### JustDeleteMe URLs returning 404
The tool fetches the latest JDM data on each run. If the cache is stale, delete it:
```bash
rm ~/.account-nuker/jdm.json
```

---



## License

MIT — see [LICENSE](LICENSE)

---

## Disclaimer

This tool accesses only accounts you own, using credentials you provide. It is intended for personal data hygiene and exercising your right to be forgotten (GDPR Art. 17, CCPA, etc.). Do not use it against accounts you do not own.
