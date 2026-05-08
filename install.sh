#!/usr/bin/env bash
# account-nuker v1.1.0 — one-line installer
# Usage: curl -sL https://raw.githubusercontent.com/YOURREPO/account-nuker/main/install.sh | bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/YOURREPO/account-nuker/main"
INSTALL_DIR="${HOME}/.local/bin"
APP_HOME="${HOME}/.account-nuker"
VENV_DIR="${APP_HOME}/venv"
APP_DEST="${INSTALL_DIR}/account-nuker"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[account-nuker]${RESET} $*"; }
success() { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "${RED}✗ ERROR:${RESET} $*" >&2; exit 1; }

echo -e "${BOLD}${CYAN}"
cat <<'BANNER'
  __ _  ___ ___ ___  _   _ _ __ | |_     _ __  _   _| | _____ _ __
 / _` |/ __/ __/ _ \| | | | '_ \| __|   | '_ \| | | | |/ / _ \ '__|
| (_| | (_| (_| (_) | |_| | | | | |_    | | | | |_| |   <  __/ |
 \__,_|\___\___\___/ \__,_|_| |_|\__|   |_| |_|\__,_|_|\_\___|_|
                                                            v1.1.0
BANNER
echo -e "${RESET}"

# ── Python check ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    warn "Python3 not found — attempting install…"
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3 python3-pip python3-venv
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3 python3-pip
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm python python-pip
    else
        error "Install Python 3.8+ manually and re-run."
    fi
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python ${PY_VER} found."

# ── venv ──────────────────────────────────────────────────────────────────────
info "Creating virtual environment at ${VENV_DIR}…"
mkdir -p "${APP_HOME}"
python3 -m venv "${VENV_DIR}"
PY="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"
success "Virtual environment ready."

# ── Python deps ───────────────────────────────────────────────────────────────
info "Installing Python dependencies…"
"${PIP}" install --quiet --upgrade pip
"${PIP}" install --quiet \
    imap-tools \
    requests \
    beautifulsoup4 \
    rich \
    textual \
    click \
    playwright
success "Python packages installed."

# ── Playwright Chromium ───────────────────────────────────────────────────────
info "Installing Playwright Chromium browser (~150 MB, one-time)…"
"${VENV_DIR}/bin/playwright" install chromium --with-deps 2>&1 \
    | grep -v "^$" | sed 's/^/  /' || true
success "Playwright Chromium installed."

# ── System deps for headful browser ──────────────────────────────────────────
if command -v apt-get &>/dev/null; then
    info "Installing headful browser system dependencies…"
    sudo apt-get install -y --no-install-recommends \
        xdg-utils \
        libgtk-3-0 \
        libasound2 \
        libx11-xcb1 \
        libxss1 \
        libxtst6 \
        libnss3 \
        libatk-bridge2.0-0 \
        libdrm2 \
        libgbm1 \
        libxkbcommon0 \
        2>/dev/null || true
    success "System dependencies installed."
fi

# ── Download app files ────────────────────────────────────────────────────────
info "Downloading application files…"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

download_file() {
    local filename="$1"
    local dest="${APP_HOME}/${filename}"
    if [[ -f "${SCRIPT_DIR}/${filename}" ]]; then
        cp "${SCRIPT_DIR}/${filename}" "${dest}"
        success "Copied ${filename} from local directory."
    else
        if command -v curl &>/dev/null; then
            curl -fsSL "${REPO_RAW}/${filename}" -o "${dest}"
        elif command -v wget &>/dev/null; then
            wget -q "${REPO_RAW}/${filename}" -O "${dest}"
        else
            error "curl or wget required."
        fi
        success "Downloaded ${filename}."
    fi
}

download_file "app.py"
download_file "browser_automation.py"

# ── Launcher ──────────────────────────────────────────────────────────────────
mkdir -p "${INSTALL_DIR}"
cat > "${APP_DEST}" << LAUNCHER
#!/usr/bin/env bash
# account-nuker launcher — auto-generated

VENV="${VENV_DIR}"
APP_DIR="${APP_HOME}"

# Pass DISPLAY/WAYLAND through for headful browser
export PYTHONPATH="\${APP_DIR}:\${PYTHONPATH:-}"

exec "\${VENV}/bin/python" "\${APP_DIR}/app.py" "\$@"
LAUNCHER
chmod +x "${APP_DEST}"
success "Launcher installed at ${APP_DEST}."

# ── PATH ──────────────────────────────────────────────────────────────────────
if [[ ":$PATH:" != *":${INSTALL_DIR}:"* ]]; then
    SHELL_RC=""
    case "${SHELL}" in
        */bash) SHELL_RC="${HOME}/.bashrc"  ;;
        */zsh)  SHELL_RC="${HOME}/.zshrc"   ;;
        */fish) SHELL_RC="${HOME}/.config/fish/config.fish" ;;
    esac
    if [[ -n "${SHELL_RC}" ]]; then
        echo "" >> "${SHELL_RC}"
        echo "# account-nuker" >> "${SHELL_RC}"
        echo "export PATH=\"\${HOME}/.local/bin:\${PATH}\"" >> "${SHELL_RC}"
        warn "Added ${INSTALL_DIR} to PATH in ${SHELL_RC}."
        warn "Run: source ${SHELL_RC}  (or open a new terminal)"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║   account-nuker v1.1.0 installed!           ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Basic scan:     ${CYAN}account-nuker${RESET}"
echo -e "  Automate:       ${CYAN}account-nuker --automate${RESET}"
echo -e "  Headless mode:  ${CYAN}account-nuker --automate --headless${RESET}"
echo -e "  Dry run test:   ${CYAN}account-nuker --dry-run${RESET}"
echo -e "  Help:           ${CYAN}account-nuker --help${RESET}"
echo ""
echo -e "${YELLOW}Gmail users:${RESET} create an App Password at"
echo "  https://myaccount.google.com/apppasswords"
echo ""
echo -e "${YELLOW}Headful browser:${RESET} requires X11 or Wayland display."
echo "  If running headless server: use ${CYAN}--headless${RESET} flag,"
echo "  or start Xvfb:  ${CYAN}Xvfb :99 -screen 0 1280x800x24 &${RESET}"
echo "                   ${CYAN}DISPLAY=:99 account-nuker --automate${RESET}"
echo ""

# ── GUI launcher ──────────────────────────────────────────────────────────────
GUI_DEST="${INSTALL_DIR}/account-nuker-gui"
cat > "${GUI_DEST}" << GUILAUNCHER
#!/usr/bin/env bash
VENV="${VENV_DIR}"
APP_DIR="${APP_HOME}"
export PYTHONPATH="\${APP_DIR}:\${PYTHONPATH:-}"
exec "\${VENV}/bin/python" "\${APP_DIR}/gui_app.py" "\$@"
GUILAUNCHER
chmod +x "${GUI_DEST}"
success "GUI launcher installed at ${GUI_DEST}."

download_file "gui_app.py"
echo ""
echo -e "  GUI mode:  ${CYAN}account-nuker-gui${RESET}   (opens browser automatically)"
