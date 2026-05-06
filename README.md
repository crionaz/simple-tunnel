# LAN Game Tunnel

A simple tunnel app that connects multiple devices over the internet to play LAN games together — similar to Moletun but stripped down to the essentials.

Works with games like **Age of Empires 2/3/4**, **Warcraft 3**, **Starcraft 2**, and other LAN-capable games.

## How It Works

```
Player A                    Server                    Player B
┌──────────┐   TCP/TLS    ┌──────────┐   TCP/TLS    ┌──────────┐
│ Game     │◄►│Wintun│◄──►│  Relay   │◄──►│Wintun│◄►│ Game     │
└──────────┘  └──────┘    └──────────┘    └──────┘  └──────────┘
```

Each client gets a virtual **Wintun** L3 network adapter (the same driver
WireGuard uses). Raw IP packets are captured and forwarded through the
relay server to all other clients, making everyone appear on the same LAN.

No ARP, no MAC layer, no driver install ceremony — `wintun.dll` is bundled
inside the executable.

## Requirements

- **Windows 10 / 11** (client)
- **Python 3.10+** (only if running from source)
- **Run as Administrator** (needed to create the virtual adapter)

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Wintun (auto-bundled)

Nothing to install — `wintun.dll` is downloaded by the GitHub Actions
build and bundled into the executable. If running from source, place
`wintun.dll` (amd64, from <https://www.wintun.net/>) next to `client.py`.

### 3. Start the server

On a VM or machine with a public IP, clone the repo and run:

```bash
./setup_server.sh
```

This single script installs Python if needed, opens the firewall port, and starts the server.

With TLS:
```bash
TLS=1 ./setup_server.sh
```

Custom port:
```bash
PORT=9000 ./setup_server.sh
```

Or run manually:
```bash
python server.py --port 21900
```

### 4. Start clients

Each player runs:

```bash
python client.py
```

In the GUI:
1. Enter the **server address** and **port**
2. Pick a **player name**
3. Choose a unique **Virtual IP** (e.g., `10.10.0.1`, `10.10.0.2`, etc.)
4. Click **Connect**

### 5. Play!

Start your game and look for LAN/multiplayer — other connected players should appear.

## TLS Encryption (Optional)

Generate certificates:

```bash
python generate_certs.py
```

Start the server with TLS:

```bash
python server.py --cert server.crt --key server.key
```

On clients, check the **"Use TLS encryption"** box.

## Auto-Reconnect

If the connection to the server drops, the client automatically reconnects with escalating backoff (1s → 2s → 5s → 10s → 15s → 30s). The TAP adapter stays open so your game doesn't lose the virtual interface. Status updates appear in the GUI.

## Building a Windows Executable

### Option A: GitHub Actions (recommended)

Push to GitHub and the `.exe` is built automatically on a real Windows runner:

```bash
git push
```

Then go to the **Actions** tab → latest run → download the **LANGameTunnel-windows** artifact.

### Option B: Build natively on Windows

Double-click `build_windows.bat` (requires Python 3.10+ in PATH). Output: `dist/LANGameTunnel.exe`.

### Option C: Build from Linux/macOS with Docker

```bash
chmod +x build.sh
./build.sh
```

> Note: Docker builds a Linux binary. For a true Windows `.exe`, use Option A or B.

Copy the `.exe` to any Windows PC — no Python installation needed. Just make sure the TAP driver is installed.

## Project Structure

```
tunnel/
├── server.py              # Relay server (run on host machine)
├── client.py              # Client with GUI + auto-reconnect
├── protocol.py            # Wire protocol (shared)
├── tap_adapter.py         # Windows TAP adapter interface
├── generate_certs.py      # TLS certificate generator
├── setup_server.sh        # One-command server setup for VMs
├── .github/workflows/    # GitHub Actions auto-build
├── build.sh               # Docker-based build (Linux/macOS)
├── build_windows.bat      # Native Windows build script
├── install_tap.bat        # TAP driver install helper
├── requirements.txt       # Python dependencies
└── README.md           # This file
```

## Network Setup Tips

- The server needs a **public IP** or **port forwarding** on port `21900`
- Each client must use a **different Virtual IP** on the same subnet (e.g., `10.10.0.X`)
- For best latency, choose a server geographically close to all players
- If your game doesn't detect other players, make sure Windows Firewall allows traffic on the TAP adapter
