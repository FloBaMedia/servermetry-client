#!/usr/bin/env bash
# ServerMetry Agent Installer for Linux
# Usage: curl -fsSL https://raw.githubusercontent.com/FloBaMedia/servermetry-client/main/agent/install.sh | bash
set -euo pipefail

GITHUB_BASE="https://raw.githubusercontent.com/FloBaMedia/servermetry-client/main/agent"
INSTALL_DIR="/etc/servermetry"
AGENT_PATH="$INSTALL_DIR/agent.py"
CONF_PATH="$INSTALL_DIR/agent.conf"
CRON_MARKER="servermetry/agent.py"
DEFAULT_API_URL="https://api.servermetry.com"

# All module files that must be present alongside agent.py
MODULE_FILES=(
    "client/__init__.py"
    "client/api.py"
    "models/__init__.py"
    "models/constants.py"
    "models/limits.py"
    "models/paths.py"
    "services/__init__.py"
    "services/config_applier.py"
    "services/linux.py"
    "services/storage.py"
    "services/darwin.py"
    "services/windows.py"
    "services/updater.py"
    "services/path_migration.py"
    "utils/__init__.py"
    "utils/config.py"
    "utils/logging.py"
    "utils/validation.py"
    "utils/lock.py"
    "utils/snapshot.py"
)

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

_download() {
    local url="$1" dest="$2"
    if command -v curl &>/dev/null; then
        curl -fsSL "$url" -o "$dest"
    else
        wget -qO "$dest" "$url"
    fi
}

# ── 1. Root check ────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    die "This installer must be run as root. Try: sudo bash install.sh"
fi

info "ServerMetry Agent Installer"
echo "─────────────────────────────────────────────"

# ── 2. Python 3.6+ check ─────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    die "Python 3.6+ not found. Install it with your package manager (e.g. apt install python3)"
fi

PY_VER=$("$PYTHON" -c "import sys; v=sys.version_info; print('{}.{}'.format(v.major, v.minor))")
info "Found Python $PY_VER at $(command -v "$PYTHON")"

# ── 3. Create install directory ───────────────────────────────────────────────
info "Creating $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR" \
         "$INSTALL_DIR/client" \
         "$INSTALL_DIR/models" \
         "$INSTALL_DIR/services" \
         "$INSTALL_DIR/utils"

if ! command -v curl &>/dev/null && ! command -v wget &>/dev/null; then
    die "Neither curl nor wget found. Install one and re-run."
fi

# ── 4. Download agent files ───────────────────────────────────────────────────
info "Downloading agent files ..."
_download "$GITHUB_BASE/agent.py" "$AGENT_PATH"
chmod 755 "$AGENT_PATH"

for mod in "${MODULE_FILES[@]}"; do
    _download "$GITHUB_BASE/$mod" "$INSTALL_DIR/$mod"
done
info "Agent installed in $INSTALL_DIR"

# ── 5. Config (env vars → existing config → interactive) ──────────────────────
# api_url is optional — agent defaults to https://api.servermetry.com unless
# SERVERPULSE_URL is set (override only).

API_URL_OVERRIDE="${SERVERMETRY_URL:-${SERVERPULSE_URL:-}}"
API_URL_OVERRIDE="${API_URL_OVERRIDE%/}"
API_KEY="${SERVERMETRY_KEY:-${SERVERPULSE_KEY:-}}"

CONF_KEY=""
if [[ -f "$CONF_PATH" ]]; then
    CONF_KEY=$(grep -E '^\s*api_key\s*=' "$CONF_PATH" 2>/dev/null \
               | sed 's/.*=\s*//' | tr -d ' \r' || true)
fi

if [[ -n "$API_KEY" ]]; then
    info "Using API Key from environment variables."
else
    [[ -n "$CONF_KEY" ]] && info "Existing config found – press Enter to keep current API Key."
    echo ""
    echo "Please enter your ServerMetry API Key:"

    if [[ -z "$API_KEY" ]]; then
        if [[ -n "$CONF_KEY" ]]; then
            MASKED="${CONF_KEY:0:10}***"
            read -rsp "  API Key [${MASKED}]: " API_KEY </dev/tty
            echo
            [[ -z "$API_KEY" ]] && API_KEY="$CONF_KEY" && info "Keeping existing API Key."
        else
            while true; do
                read -rsp "  API Key (sp_live_...): " API_KEY </dev/tty
                echo
                if [[ ${#API_KEY} -ge 8 ]]; then
                    break
                fi
                warn "API key seems too short. Please try again."
            done
        fi
    fi
fi

if [[ -n "$API_URL_OVERRIDE" ]]; then
    info "API URL override from environment: $API_URL_OVERRIDE"
else
    info "Using default API URL: $DEFAULT_API_URL"
fi

# ── 6. Write config ───────────────────────────────────────────────────────────
{
    echo "[servermetry]"
    echo "api_key = $API_KEY"
    if [[ -n "$API_URL_OVERRIDE" ]]; then
        echo "api_url = $API_URL_OVERRIDE"
    fi
} > "$CONF_PATH"
chmod 600 "$CONF_PATH"
info "Config written to $CONF_PATH (mode 600)"

# ── 7. Ensure crontab is available ────────────────────────────────────────────
# Minimal Debian/cloud images often ship without the `cron` package.
_ensure_crontab() {
    if command -v crontab &>/dev/null; then
        return 0
    fi

    warn "crontab not found – installing cron package ..."
    if command -v apt-get &>/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cron
    elif command -v dnf &>/dev/null; then
        dnf install -y -q cronie
    elif command -v yum &>/dev/null; then
        yum install -y -q cronie
    elif command -v zypper &>/dev/null; then
        zypper --non-interactive install -y cron
    elif command -v apk &>/dev/null; then
        apk add --no-cache cronie 2>/dev/null || apk add --no-cache dcron
    else
        die "crontab not found and no supported package manager. Install cron (Debian/Ubuntu: apt install cron) and re-run the installer."
    fi

    if ! command -v crontab &>/dev/null; then
        die "crontab still not available after package install."
    fi
    info "crontab is now available."

    # Start the cron daemon (package install may not auto-start it).
    if command -v systemctl &>/dev/null; then
        systemctl enable --now cron 2>/dev/null \
            || systemctl enable --now crond 2>/dev/null \
            || true
    elif command -v rc-service &>/dev/null; then
        # Alpine / OpenRC – systemctl is usually absent (cronie→crond, dcron→dcron)
        rc-update add crond default 2>/dev/null \
            || rc-update add dcron default 2>/dev/null \
            || rc-update add cron default 2>/dev/null \
            || true
        rc-service crond start 2>/dev/null \
            || rc-service dcron start 2>/dev/null \
            || rc-service cron start 2>/dev/null \
            || true
    elif command -v service &>/dev/null; then
        service cron start 2>/dev/null \
            || service crond start 2>/dev/null \
            || service dcron start 2>/dev/null \
            || true
    fi
}

_ensure_crontab

# ── 8. Crontab entry (idempotent) ─────────────────────────────────────────────
CRON_LINE="* * * * * $PYTHON $AGENT_PATH"

if crontab -l 2>/dev/null | grep -qF "$CRON_MARKER"; then
    info "Crontab entry already exists – skipping."
else
    # `|| true` prevents set -e from aborting when no crontab exists yet (exit 1)
    { crontab -l 2>/dev/null || true; echo "$CRON_LINE"; } | crontab -
    info "Added crontab entry: $CRON_LINE"
fi

# ── 9. First test run ─────────────────────────────────────────────────────────
echo ""
info "Verifying agent (collecting metrics, no HTTP request) ..."
echo "─────────────────────────────────────────────"
if "$PYTHON" "$AGENT_PATH" --check; then
    echo "─────────────────────────────────────────────"
    info "Metrics collection: OK"
else
    echo "─────────────────────────────────────────────"
    warn "Agent check failed — verify Python and config, then run:"
    warn "  $PYTHON $AGENT_PATH --check"
fi

echo ""
info "Installation complete!"
info "The agent will run every minute via crontab."
info "Logs: /var/log/servermetry-agent.log"
echo ""
echo "To test a live run now (sends real data to your API):"
echo "  sudo $PYTHON $AGENT_PATH"
echo ""
echo "To view logs:"
echo "  tail -f /var/log/servermetry-agent.log"
