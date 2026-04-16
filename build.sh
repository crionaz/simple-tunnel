#!/usr/bin/env bash
# =============================================================================
#  LAN Game Tunnel - Build Script
#
#  Run this on an Ubuntu/Debian VM to produce a Windows .exe installer.
#  It will:
#    1. Install system dependencies (Wine, Python for Windows)
#    2. Install pip packages inside Wine's Python
#    3. Package the client into a single .exe with PyInstaller
#
#  Usage:
#    chmod +x build.sh
#    ./build.sh
#
#  Output:
#    dist/LANGameTunnel.exe   — single-file Windows executable
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_tmp"
DIST_DIR="$SCRIPT_DIR/dist"
PYTHON_WIN_VERSION="3.11.9"
PYTHON_WIN_URL="https://www.python.org/ftp/python/${PYTHON_WIN_VERSION}/python-${PYTHON_WIN_VERSION}-amd64.exe"
PYTHON_WIN_EXE="$BUILD_DIR/python-installer.exe"

echo "============================================"
echo " LAN Game Tunnel - Windows Build"
echo "============================================"
echo ""

# ---- 1. Install system deps ------------------------------------------------

echo "[1/5] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq wine64 wget xvfb > /dev/null 2>&1
echo "  Done."

# ---- 2. Set up Wine + Windows Python ---------------------------------------

echo "[2/5] Setting up Wine prefix..."
export WINEPREFIX="$BUILD_DIR/wine_prefix"
export WINEDEBUG=-all
mkdir -p "$BUILD_DIR" "$DIST_DIR"

# Initialize wine prefix silently
if [ ! -d "$WINEPREFIX/drive_c" ]; then
    wineboot --init > /dev/null 2>&1 || true
    sleep 3
fi

# Download Windows Python if needed
WINE_PYTHON="$WINEPREFIX/drive_c/Python311/python.exe"
if [ ! -f "$WINE_PYTHON" ]; then
    echo "[2/5] Downloading Windows Python ${PYTHON_WIN_VERSION}..."
    wget -q -O "$PYTHON_WIN_EXE" "$PYTHON_WIN_URL"

    echo "[2/5] Installing Python inside Wine (silent install)..."
    # Use Xvfb for headless install
    xvfb-run wine "$PYTHON_WIN_EXE" /quiet InstallAllUsers=0 \
        TargetDir="C:\\Python311" PrependPath=1 \
        Include_test=0 Include_launcher=0 > /dev/null 2>&1 || true
    sleep 2
    echo "  Done."
else
    echo "  Windows Python already installed."
fi

# Verify python works
wine "$WINE_PYTHON" --version 2>/dev/null || {
    echo "ERROR: Wine Python installation failed."
    echo "You can try the native build method instead — see below."
    exit 1
}

# ---- 3. Install pip packages -----------------------------------------------

echo "[3/5] Installing Python packages..."
wine "$WINE_PYTHON" -m pip install --quiet --upgrade pip > /dev/null 2>&1
wine "$WINE_PYTHON" -m pip install --quiet pyinstaller > /dev/null 2>&1
echo "  Done."

# ---- 4. Build with PyInstaller ----------------------------------------------

echo "[4/5] Building Windows executable with PyInstaller..."

# Create a PyInstaller spec for a clean single-file build
cat > "$BUILD_DIR/tunnel.spec" << 'SPEC_EOF'
# -*- mode: python ; coding: utf-8 -*-
import os

src = os.environ.get('TUNNEL_SRC', '.')

a = Analysis(
    [os.path.join(src, 'client.py')],
    pathex=[src],
    datas=[],
    hiddenimports=['tkinter', 'tkinter.ttk'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LANGameTunnel',
    debug=False,
    strip=False,
    upx=True,
    console=False,          # No console window — GUI only
    icon=None,
)
SPEC_EOF

export TUNNEL_SRC="$SCRIPT_DIR"
cd "$BUILD_DIR"

wine "$WINE_PYTHON" -m PyInstaller \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR/pyinstaller_work" \
    --specpath "$BUILD_DIR" \
    --noconfirm \
    "$BUILD_DIR/tunnel.spec" 2>&1 | tail -5

echo "  Done."

# ---- 5. Summary ------------------------------------------------------------

echo ""
echo "[5/5] Build complete!"
echo ""

if [ -f "$DIST_DIR/LANGameTunnel.exe" ]; then
    SIZE=$(du -h "$DIST_DIR/LANGameTunnel.exe" | cut -f1)
    echo "  Output: $DIST_DIR/LANGameTunnel.exe ($SIZE)"
    echo ""
    echo "  Copy LANGameTunnel.exe to any Windows PC and double-click to run."
    echo "  (TAP-Windows driver must be installed separately — see install_tap.bat)"
else
    echo "  WARNING: .exe not found. Check build output for errors."
    echo ""
    echo "  ALTERNATIVE: Build natively on Windows instead:"
    echo "    1. Install Python 3.10+ on Windows"
    echo "    2. pip install pyinstaller"
    echo "    3. Run:  build_windows.bat"
fi

echo ""
echo "============================================"
