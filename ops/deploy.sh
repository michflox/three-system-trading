#!/usr/bin/env bash
# ops/deploy.sh — Idempotent Ubuntu bootstrap for the crypto DRY_RUN/PAPER engine.
#
# Run as root on Ubuntu 22.04 or 24.04.
# Safe to re-run: every step checks for existing state before acting.
#
# LIVE MODE IS NOT ENABLED AND CANNOT BE ENABLED BY THIS SCRIPT.
# LIVE activation is a human-only, post-eight-week-PAPER action.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/michflox/three-system-trading.git"
REPO_BRANCH="main"
INSTALL_DIR="/opt/trading-bot"
SERVICE_USER="tradingbot"
SERVICE_GROUP="tradingbot"
DATA_DIR="/var/lib/trading-bot"
LOG_DIR="/var/log/trading-bot"
CONF_DIR="/etc/trading-bot"
SYSTEMD_DIR="/etc/systemd/system"

# ---------------------------------------------------------------------------
# Guard: must run as root
# ---------------------------------------------------------------------------
if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: run this script as root: sudo bash ops/deploy.sh" >&2
    exit 1
fi

echo "==================================================================="
echo "  three-system-trading — Ubuntu deployment bootstrap"
echo "  Install : $INSTALL_DIR"
echo "  User    : $SERVICE_USER"
echo "  Mode    : DRY_RUN only (LIVE is permanently blocked by this script)"
echo "==================================================================="
echo ""

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
step() { echo ""; echo "--- [$1/$TOTAL_STEPS] $2 ---"; }
TOTAL_STEPS=8

step 1 "System packages"
apt-get update -q
apt-get install -y -q git python3 python3-venv python3-pip

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo "ERROR: Python 3.11+ required; found $PY_VER" >&2
    exit 1
fi
python3 --version

# ---------------------------------------------------------------------------
# 2. System user
# ---------------------------------------------------------------------------
step 2 "System user"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --comment "Trading bot service account" "$SERVICE_USER"
    echo "Created user: $SERVICE_USER"
else
    echo "User $SERVICE_USER already exists — skipping"
fi

# ---------------------------------------------------------------------------
# 3. Directories
# ---------------------------------------------------------------------------
step 3 "Directories"
for dir in \
    "$DATA_DIR/data" \
    "$DATA_DIR/reports" \
    "$DATA_DIR/state" \
    "$DATA_DIR/run" \
    "$LOG_DIR" \
    "$CONF_DIR"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir"
        echo "Created: $dir"
    else
        echo "Exists:  $dir"
    fi
done

chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR" "$LOG_DIR"
chmod 750 "$DATA_DIR" "$LOG_DIR"
chmod 750 "$CONF_DIR"
chown root:root "$CONF_DIR"

# ---------------------------------------------------------------------------
# 4. Clone or update repository
# ---------------------------------------------------------------------------
step 4 "Repository"
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    echo "Cloned $REPO_URL -> $INSTALL_DIR"
else
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_BRANCH"
    echo "Updated $INSTALL_DIR to latest $REPO_BRANCH"
fi
chown -R root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 5. Python virtual environment
# ---------------------------------------------------------------------------
step 5 "Virtual environment"
if [[ ! -f "$INSTALL_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/.venv"
    echo "Created venv"
else
    echo "Venv exists — upgrading dependencies"
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
# Install runtime dependencies only; dev extras are not required on the server.
"$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"
echo "Dependencies installed"

# ---------------------------------------------------------------------------
# 6. Environment files (placeholder secrets only — never real credentials)
# ---------------------------------------------------------------------------
step 6 "Environment files"

install_env() {
    local src="$1" dst="$2"
    if [[ ! -f "$dst" ]]; then
        cp "$src" "$dst"
        chmod 600 "$dst"
        chown root:root "$dst"
        echo "Created $dst — FILL IN SECRETS BEFORE STARTING"
    else
        echo "Exists: $dst — NOT overwritten; verify it is current against $src"
    fi
}

install_env \
    "$INSTALL_DIR/ops/systemd/crypto-paper.env.example" \
    "$CONF_DIR/crypto-paper.env"

install_env \
    "$INSTALL_DIR/ops/systemd/data-recorder.env.example" \
    "$CONF_DIR/data-recorder.env"

# Enforce DRY_RUN in the env file regardless of what the example says.
# This is a hard guard: LIVE and PAPER are not permitted at bootstrap time.
if grep -q "^TRADING_EXECUTION_MODE=" "$CONF_DIR/crypto-paper.env"; then
    sed -i "s|^TRADING_EXECUTION_MODE=.*|TRADING_EXECUTION_MODE=DRY_RUN|" \
        "$CONF_DIR/crypto-paper.env"
else
    echo "TRADING_EXECUTION_MODE=DRY_RUN" >> "$CONF_DIR/crypto-paper.env"
fi
echo "TRADING_EXECUTION_MODE locked to DRY_RUN in $CONF_DIR/crypto-paper.env"

# ---------------------------------------------------------------------------
# 7. Systemd unit files
# ---------------------------------------------------------------------------
step 7 "Systemd unit files"

install_unit() {
    local name="$1"
    local src="$INSTALL_DIR/ops/systemd/$name"
    local dst="$SYSTEMD_DIR/$name"
    if [[ ! -f "$src" ]]; then
        echo "WARNING: $src not found — skipping" >&2
        return
    fi
    cp "$src" "$dst"
    echo "Installed: $dst"
}

for unit in \
    trading-crypto-paper.service \
    trading-crypto-diff.service \
    trading-crypto-diff.timer \
    trading-crypto-quality.service \
    trading-crypto-quality.timer; do
    install_unit "$unit"
done

# Install the data-recorder unit file but do NOT enable it at bootstrap.
# The funding endpoint (api.exchange.fairx.net/rest/funding-rate) is public;
# the permission gate uses the CDP key to call /api/v3/brokerage/key_permissions.
# Fill in COINBASE_API_KEY and COINBASE_API_SECRET in data-recorder.env and run
# 'python -m data.recorder backfill' to verify before enabling this service.
install_unit "trading-data-recorder.service"
echo "NOTE: trading-data-recorder.service installed but NOT enabled (fill secrets first)"

systemctl daemon-reload
echo "systemd reloaded"

# ---------------------------------------------------------------------------
# 8. Enable services (paper engine + timers only; data recorder stays off)
# ---------------------------------------------------------------------------
step 8 "Enable services"
systemctl enable trading-crypto-paper.service
systemctl enable trading-crypto-diff.timer
systemctl enable trading-crypto-quality.timer
echo "Enabled: trading-crypto-paper.service, trading-crypto-diff.timer, trading-crypto-quality.timer"
echo "NOT enabled: trading-data-recorder.service"

# ---------------------------------------------------------------------------
# Completion checklist
# ---------------------------------------------------------------------------
cat <<'CHECKLIST'

===================================================================
  MANUAL STEPS — complete these before starting the service
===================================================================

[SECURITY] Revoke and replace the previously exposed Coinbase API key
  A plaintext Coinbase API key was exposed in the repository working
  tree (file get_coinbase_fee.py, since deleted; never committed).
  Before any deployment:

  1. Log in to https://portal.cdp.coinbase.com
  2. Locate and DELETE the exposed key:
       Name/label: ThreeBotTrade
       UUID prefix: 072b9c62-ebd1-4a6f-a1f4-8d92cd22630a
  3. Create a replacement CDP key with VIEW permission only.
     (No trading. No withdrawal. No transfer.)
  4. Download the replacement JSON; extract maker_fee_rate via:
       GET /api/v3/brokerage/transaction_summary   (authenticated)
  5. CdpJwtAuth accepts either an EC or Ed25519 PEM private key natively.
     If your key JSON gives a raw (non-PEM) base64 privateKey instead of
     PEM, the adapter does NOT support that format yet — stop and get a
     PEM-formatted key, or the key parse will fail with a PEM framing
     error (ValueError: Unable to load PEM file).
  6. Multi-line PEM secrets in an EnvironmentFile must use double-quoted
     real line breaks, not "\n"-escaped single-line text — systemd's
     EnvironmentFile= parser strips "\n" escapes silently. See
     ops/systemd/data-recorder.env.example.

[SECRETS] Edit /etc/trading-bot/crypto-paper.env  (mode 0600)
  Required values:
    COINBASE_MAKER_FEE=     <- from authenticated transaction_summary
    COINBASE_TAKER_FEE=     <- from authenticated transaction_summary
    KRAKEN_MAKER_FEE=       <- re-verify; Kraken changed tiers 2026-07-09
    KRAKEN_TAKER_FEE=       <- re-verify
    TELEGRAM_BOT_TOKEN=     <- from @BotFather
    TELEGRAM_CHAT_ID=       <- your chat or channel ID

  TRADING_EXECUTION_MODE is locked to DRY_RUN by this script.
  Do NOT change it to PAPER or LIVE before completing the DRY_RUN gate.

[DRY_RUN GATE] 48 continuous hours — required before any PAPER use
  Start the service:
    systemctl start trading-crypto-paper.service
    journalctl -u trading-crypto-paper -f

  Pass criteria (all required):
    - Service remains active for 48+ continuous hours
    - Heartbeat file advances at least every 5 minutes:
        /var/lib/trading-bot/run/crypto-paper-heartbeat.json
    - Zero unhandled exceptions in the journal
    - Daily cycle record written at 00:05 UTC
    - Quality oneshot completes at 00:12 UTC
    - Diff oneshot completes at 00:15 UTC
    - Kill drill performed and documented:
        systemctl kill --signal=KILL trading-crypto-paper
        Verify automatic restart + position/order reconciliation

[PAPER GATE] 8 uninterrupted weeks — required before any LIVE discussion
  Only after 48-hour DRY_RUN evidence is signed off:
    sudo sed -i 's/TRADING_EXECUTION_MODE=DRY_RUN/TRADING_EXECUTION_MODE=PAPER/' \
        /etc/trading-bot/crypto-paper.env
    systemctl restart trading-crypto-paper.service

  Earliest LIVE discussion: 2026-09-01
  (moves to 56 days after actual PAPER start if PAPER starts later
  or is interrupted)

  THE AGENT NEVER ENABLES LIVE. Live mode is a human-only action.

===================================================================
CHECKLIST

echo ""
echo "Bootstrap complete. Follow the checklist above before starting the service."
