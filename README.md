# LAN Game Tunnel

A simple tunnel app that connects multiple devices over the internet to play LAN games together — similar to Moletun but stripped down to the essentials.

Works with games like **Age of Empires 2/3/4**, **Warcraft 3**, **Starcraft 2**, and other LAN-capable games.

## How It Works

```
Player A                    Server                    Player B
┌──────────┐    TCP/TLS     ┌──────────┐    TCP/TLS   ┌──────────┐
│ Game     │◄──►│ TAP  │◄──►│  Relay   │◄──►│ TAP  │◄──►│ Game     │
│          │    │Adapter│    │  Server  │    │Adapter│    │          │
└──────────┘    └──────┘    └──────────┘    └──────┘    └──────────┘
```

Each client gets a virtual TAP network adapter. Ethernet frames are captured and forwarded through the relay server to all other clients, making everyone appear on the same LAN.

## Requirements

- **Windows** (client)
- **Python 3.10+**
- **TAP-Windows driver** (from OpenVPN) — run `install_tap.bat` for instructions

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Install TAP driver (clients only)

Run `install_tap.bat` or install [OpenVPN](https://openvpn.net/community-downloads/) (which includes the TAP driver).

### 3. Start the server

On a machine with a public IP (or port-forwarded):

```bash
python server.py
```

Options:
```
--host 0.0.0.0    Bind address (default: 0.0.0.0)
--port 21900      Port (default: 21900)
--cert FILE       TLS certificate (optional)
--key  FILE       TLS private key (optional)
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

### Option A: Build on a Linux VM (cross-compile)

```bash
chmod +x build.sh
./build.sh
```

This installs Wine + Windows Python, then uses PyInstaller to produce `dist/LANGameTunnel.exe`.

### Option B: Build natively on Windows

Double-click `build_windows.bat` (requires Python 3.10+ in PATH). Output: `dist/LANGameTunnel.exe`.

Copy the `.exe` to any Windows PC — no Python installation needed. Just make sure the TAP driver is installed.

## Project Structure

```
tunnel/
├── server.py           # Relay server (run on host machine)
├── client.py           # Client with GUI + auto-reconnect
├── protocol.py         # Wire protocol (shared)
├── tap_adapter.py      # Windows TAP adapter interface
├── generate_certs.py   # TLS certificate generator
├── build.sh            # Linux VM → Windows .exe cross-build
├── build_windows.bat   # Native Windows build script
├── install_tap.bat     # TAP driver install helper
├── requirements.txt    # Python dependencies
└── README.md           # This file
```

## Network Setup Tips

- The server needs a **public IP** or **port forwarding** on port `21900`
- Each client must use a **different Virtual IP** on the same subnet (e.g., `10.10.0.X`)
- For best latency, choose a server geographically close to all players
- If your game doesn't detect other players, make sure Windows Firewall allows traffic on the TAP adapter
