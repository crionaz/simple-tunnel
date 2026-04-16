#!/usr/bin/env bash
# =============================================================================
#  LAN Game Tunnel - Server Setup & Run
#
#  Run this on any Linux VM (Ubuntu/Debian) to set up and start the server.
#  It installs Python if needed, opens the firewall port, and starts serving.
#
#  Usage:
#    curl -sSL <your-raw-url>/setup_server.sh | bash
#    # or
#    chmod +x setup_server.sh
#    ./setup_server.sh
#
#  Options (environment variables):
#    PORT=21900          Server port (default: 21900)
#    TLS=1               Enable TLS (auto-generates certs if missing)
# =============================================================================

set -euo pipefail

PORT="${PORT:-21900}"
TLS="${TLS:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================"
echo " LAN Game Tunnel - Server Setup"
echo "============================================"
echo ""

# ---- 1. Install Python if missing -----------------------------------------

if ! command -v python3 &> /dev/null; then
    echo "[1/4] Installing Python 3..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip > /dev/null
    elif command -v yum &> /dev/null; then
        sudo yum install -y python3 python3-pip > /dev/null
    elif command -v dnf &> /dev/null; then
        sudo dnf install -y python3 python3-pip > /dev/null
    else
        echo "ERROR: Could not install Python. Please install Python 3.10+ manually."
        exit 1
    fi
    echo "  Done."
else
    echo "[1/4] Python 3 found: $(python3 --version)"
fi

# ---- 2. Open firewall port ------------------------------------------------

echo "[2/4] Configuring firewall for port $PORT..."
if command -v ufw &> /dev/null; then
    sudo ufw allow "$PORT/tcp" > /dev/null 2>&1 || true
    echo "  ufw: port $PORT opened"
elif command -v firewall-cmd &> /dev/null; then
    sudo firewall-cmd --permanent --add-port="$PORT/tcp" > /dev/null 2>&1 || true
    sudo firewall-cmd --reload > /dev/null 2>&1 || true
    echo "  firewalld: port $PORT opened"
else
    echo "  No firewall manager detected — make sure port $PORT is open"
fi

# ---- 3. Generate TLS certs if requested -----------------------------------

CERT_ARGS=""
if [ "$TLS" = "1" ]; then
    echo "[3/4] Setting up TLS..."
    if [ ! -f "$SCRIPT_DIR/server.crt" ] || [ ! -f "$SCRIPT_DIR/server.key" ]; then
        # Install cryptography — try system package first (avoids PEP 668 issues),
        # fall back to pip with --break-system-packages for newer distros
        if command -v apt-get &> /dev/null; then
            sudo apt-get install -y -qq python3-cryptography > /dev/null 2>&1 || true
        fi
        # Check if it imported successfully, if not try pip
        if ! python3 -c "import cryptography" 2>/dev/null; then
            pip3 install --break-system-packages --quiet cryptography 2>/dev/null \
                || pip3 install --quiet cryptography 2>/dev/null \
                || pip install --quiet cryptography 2>/dev/null \
                || { echo "  ERROR: Could not install cryptography. Install manually:"; \
                     echo "    sudo apt-get install python3-cryptography"; exit 1; }
        fi
        cd "$SCRIPT_DIR"
        python3 generate_certs.py
    else
        echo "  Certificates already exist"
    fi
    CERT_ARGS="--cert server.crt --key server.key"
else
    echo "[3/4] TLS disabled (set TLS=1 to enable)"
fi

# ---- 4. Start server ------------------------------------------------------

echo "[4/4] Starting server on port $PORT..."
echo ""
echo "  Players connect to:  <your-vm-ip>:$PORT"
echo "  Press Ctrl+C to stop"
echo ""
echo "============================================"
echo ""

cd "$SCRIPT_DIR"
exec python3 server.py --port "$PORT" $CERT_ARGS
