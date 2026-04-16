#!/usr/bin/env bash
# =============================================================================
#  LAN Game Tunnel - Build Script
#
#  Builds a standalone Windows .exe from a Linux/macOS machine using Docker.
#  No Wine needed — uses a real Windows Python inside a Docker container.
#
#  Prerequisites: Docker installed and running
#
#  Usage:
#    chmod +x build.sh
#    ./build.sh
#
#  Output:
#    dist/LANGameTunnel.exe
#
#  Alternative (no Docker):
#    Push to GitHub and let the Actions workflow build it automatically.
#    See .github/workflows/build.yml
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"

echo "============================================"
echo " LAN Game Tunnel - Windows Build"
echo "============================================"
echo ""

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed or not in PATH."
    echo ""
    echo "Install Docker:"
    echo "  Ubuntu/Debian:  sudo apt-get install docker.io"
    echo "  macOS:          brew install --cask docker"
    echo ""
    echo "Or use one of these alternatives:"
    echo "  1. Push to GitHub → Actions builds the .exe automatically"
    echo "  2. Build natively on Windows → run build_windows.bat"
    exit 1
fi

mkdir -p "$DIST_DIR"

# ---- Build inside Docker ---------------------------------------------------

echo "[1/2] Building Windows .exe inside Docker container..."
echo "      (first run downloads the image — may take a few minutes)"
echo ""

docker run --rm \
    -v "$SCRIPT_DIR":/src \
    -w /src \
    python:3.11-slim \
    bash -c '
        set -e
        echo "  Installing PyInstaller..."
        pip install --quiet pyinstaller 2>/dev/null

        echo "  Packaging LANGameTunnel..."
        pyinstaller --noconfirm --onefile --noconsole --uac-admin \
            --name LANGameTunnel \
            --distpath /src/dist \
            --workpath /tmp/build_work \
            --specpath /tmp \
            client.py 2>&1 | tail -3

        echo "  Done."
    '

# ---- Summary ---------------------------------------------------------------

echo ""
echo "[2/2] Build complete!"
echo ""

if [ -f "$DIST_DIR/LANGameTunnel" ] || [ -f "$DIST_DIR/LANGameTunnel.exe" ]; then
    OUTPUT=$(ls "$DIST_DIR"/LANGameTunnel* 2>/dev/null | head -1)
    SIZE=$(du -h "$OUTPUT" | cut -f1)
    echo "  Output: $OUTPUT ($SIZE)"
    echo ""
    # If built on Linux, the output is a Linux binary — note this
    if [[ "$(uname)" != MINGW* ]] && [[ "$(uname)" != CYGWIN* ]]; then
        echo "  NOTE: This was built on $(uname) — the binary runs on $(uname)."
        echo "  For a Windows .exe, use one of these methods:"
        echo ""
        echo "  Option A — GitHub Actions (recommended):"
        echo "    git push  →  Go to Actions tab  →  Download artifact"
        echo ""
        echo "  Option B — Build on Windows directly:"
        echo "    Run build_windows.bat on a Windows machine"
    else
        echo "  Copy LANGameTunnel.exe to any Windows PC and double-click to run."
        echo "  (TAP-Windows driver must be installed separately — see install_tap.bat)"
    fi
else
    echo "  Build output not found. Check Docker output above for errors."
fi

echo ""
echo "============================================"
