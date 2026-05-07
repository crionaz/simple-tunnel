"""LAN Game Tunnel - Client with GUI.

Connects to the relay server and bridges the local TAP virtual
adapter with remote peers, creating a shared virtual LAN.
"""

import json
import socket
import ssl
import sys
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import logging

from protocol import (
    HEADER_SIZE, DEFAULT_PORT,
    MSG_DATA, MSG_HELLO, MSG_INFO, MSG_KEEPALIVE, MSG_PEERS, MSG_QUERY,
    MSG_PING, MSG_PONG,
    pack_message, unpack_header,
)
from wintun_adapter import WintunAdapter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('tunnel-client')

# Config file location (next to the executable / script)
_CONFIG_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
_CONFIG_FILE = os.path.join(_CONFIG_DIR, 'tunnel_config.json')


# ---------------------------------------------------------------------------
# Network client
# ---------------------------------------------------------------------------
class TunnelClient:
    RECONNECT_DELAYS = [1, 2, 5, 10, 15, 30]  # seconds, escalating backoff

    def __init__(self, on_status=None, on_peers=None):
        self.sock: socket.socket | None = None
        self.tap: WintunAdapter | None = None
        self.running = False
        self._threads: list[threading.Thread] = []
        self._on_status = on_status  # callback(str)
        self._on_peers = on_peers    # callback(list[dict])
        # Stored for reconnection
        self._host: str = ''
        self._port: int = DEFAULT_PORT
        self._name: str = ''
        self._ip_addr: str = ''
        self._use_tls: bool = False
        self._reconnect_count: int = 0
        self._lock = threading.Lock()
        self._connected_at: float = 0  # time.monotonic() of last connect
        self._on_error = None          # callback(str) for fatal errors
        # Peer ping bookkeeping: { peer_ip: (rtt_ms, recorded_at_monotonic) }
        self.peer_rtt: dict[str, tuple[float, float]] = {}
        self._known_peers: list[dict] = []
        # Frame counters
        self.frames_to_server = 0
        self.frames_from_server = 0
        # Per-protocol packet counters (parsed from IP header — Wintun is L3,
        # so no Ethernet/ARP layer to worry about).
        # Keys: 'icmp_echo_req', 'icmp_echo_reply', 'icmp_other',
        #       'tcp', 'udp', 'ipv6', 'other'
        self.proto_to: dict[str, int] = {}
        self.proto_from: dict[str, int] = {}

    def connect(self, host: str, port: int, name: str, preferred_ip: str,
                use_tls: bool = False, subnet_pref: str = '') -> str:
        """Connect to the relay server, send HELLO, return the assigned virtual IP."""
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        raw.settimeout(10)

        if use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                self.sock = ctx.wrap_socket(raw)
                self.sock.connect((host, port))
            except ssl.SSLError as e:
                raw.close()
                raise ConnectionError(
                    f'TLS handshake failed.\n\n'
                    f'Make sure the server is running with TLS enabled:\n'
                    f'  TLS=1 bash setup_server.sh\n\n'
                    f'Detail: {e}'
                ) from None
        else:
            self.sock = raw
            self.sock.connect((host, port))

        self._connected_at = time.monotonic()
        hello = json.dumps({
            'name': name,
            'ip': preferred_ip,
            'subnet': subnet_pref,
        }).encode('utf-8')
        self.sock.sendall(pack_message(MSG_HELLO, hello))

        # Wait for server to respond with assigned IP (or error)
        assigned_ip = self._wait_for_assignment()
        self.sock.settimeout(None)
        log.info('Connected to %s:%d, assigned IP %s', host, port, assigned_ip)
        return assigned_ip

    def _wait_for_assignment(self) -> str:
        """Read messages until MSG_INFO with assigned_ip arrives. Discard MSG_PEERS."""
        while True:
            header = self._recv_exact(HEADER_SIZE)
            length, msg_type = unpack_header(header)
            payload = self._recv_exact(length) if length > 0 else b''
            if msg_type == MSG_INFO:
                try:
                    info = json.loads(payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if 'error' in info:
                    raise ConnectionError(info['error'])
                if 'assigned_ip' in info:
                    return info['assigned_ip']
            # Ignore other early messages (peers list, etc.)

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly n bytes from the socket."""
        data = bytearray()
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError('Server closed connection')
            data.extend(chunk)
        return bytes(data)

    def _set_status(self, msg: str):
        log.info(msg)
        if self._on_status:
            self._on_status(msg)

    def _close_socket(self):
        with self._lock:
            if self.sock:
                # Shutdown first to immediately unblock any blocked recv()
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None

    def _reconnect(self):
        """Attempt to reconnect with escalating backoff."""
        while self.running:
            delay = self.RECONNECT_DELAYS[min(self._reconnect_count, len(self.RECONNECT_DELAYS) - 1)]
            self._set_status(f'Reconnecting in {delay}s...')
            for _ in range(delay * 10):  # sleep in 0.1s increments so we can bail fast
                if not self.running:
                    return False
                time.sleep(0.1)
            try:
                self._close_socket()
                assigned = self.connect(self._host, self._port, self._name,
                                        self._ip_addr, self._use_tls,
                                        subnet_pref=getattr(self, '_subnet_pref', ''))
                # Re-configure TAP if server gave us a different IP
                if assigned and assigned != self._ip_addr and self.tap:
                    try:
                        self.tap.configure_ip(assigned)
                    except Exception:
                        log.exception('Failed to reconfigure TAP IP')
                    self._ip_addr = assigned
                self._reconnect_count = 0
                self._set_status(f'Reconnected ({self._ip_addr})')
                return True
            except Exception as e:
                self._reconnect_count += 1
                log.warning('Reconnect attempt failed: %s', e)
        return False

    @staticmethod
    def _classify_frame(pkt: bytes) -> str:
        """Return short protocol tag for a raw IP packet (Wintun is L3)."""
        if len(pkt) < 1:
            return 'other'
        ver = pkt[0] >> 4
        if ver == 6:
            return 'ipv6'
        if ver != 4 or len(pkt) < 20:
            return 'other'
        ip_proto = pkt[9]
        if ip_proto == 1:  # ICMP
            ihl = (pkt[0] & 0x0F) * 4
            if len(pkt) > ihl:
                t = pkt[ihl]
                if t == 8:
                    return 'icmp_echo_req'
                if t == 0:
                    return 'icmp_echo_reply'
            return 'icmp_other'
        if ip_proto == 6:
            return 'tcp'
        if ip_proto == 17:
            return 'udp'
        return 'other'

    def _tap_to_server(self):
        """Read Ethernet frames from TAP and send to server."""
        while self.running:
            try:
                frame = self.tap.read()
                if frame and self.running:
                    with self._lock:
                        if self.sock:
                            self.sock.sendall(pack_message(MSG_DATA, frame))
                            self.frames_to_server += 1
                            tag = self._classify_frame(frame)
                            self.proto_to[tag] = self.proto_to.get(tag, 0) + 1
            except OSError:
                if self.running:
                    log.error('TAP → server relay failed')
                    # Wait for server_to_tap thread to handle reconnect
                    time.sleep(1)
            except Exception:
                if self.running:
                    log.exception('TAP read error')
                break

    def _server_to_tap(self):
        """Read Ethernet frames from server and write to TAP. Drives reconnection."""
        while self.running:
            try:
                header = self._recv_exact(HEADER_SIZE)
                length, msg_type = unpack_header(header)
                payload = self._recv_exact(length) if length > 0 else b''

                if msg_type == MSG_DATA and self.tap:
                    self.frames_from_server += 1
                    tag = self._classify_frame(payload)
                    self.proto_from[tag] = self.proto_from.get(tag, 0) + 1
                    self.tap.write(payload)
                elif msg_type == MSG_PING:
                    # Another peer is pinging us — reply with PONG if 'to' matches our IP
                    try:
                        info = json.loads(payload)
                        if info.get('to') == self._ip_addr:
                            with self._lock:
                                if self.sock:
                                    self.sock.sendall(pack_message(MSG_PONG, payload))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                elif msg_type == MSG_PONG:
                    # Our ping came back
                    try:
                        info = json.loads(payload)
                        if info.get('from') == self._ip_addr:
                            sent = info.get('ts', 0)
                            rtt = (time.monotonic() - sent) * 1000
                            peer_ip = info.get('to', '')
                            # Discard nonsense RTTs (negative, huge, or stale)
                            if 0 <= rtt < 30000 and peer_ip:
                                self.peer_rtt[peer_ip] = (rtt, time.monotonic())
                                log.info('PONG from %s: %.1f ms', peer_ip, rtt)
                                # Re-render peers with new RTT
                                if self._on_peers and self._known_peers:
                                    self._on_peers(self._known_peers)
                            else:
                                log.warning('Ignoring stale/bogus PONG: rtt=%.1fms peer=%s',
                                            rtt, peer_ip)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                elif msg_type == MSG_PEERS:
                    try:
                        peers = json.loads(payload)
                        self._known_peers = peers
                        # Drop RTT entries for peers no longer present
                        current_ips = {p.get('ip', '') for p in peers}
                        for ip in list(self.peer_rtt.keys()):
                            if ip not in current_ips:
                                self.peer_rtt.pop(ip, None)
                        if self._on_peers:
                            self._on_peers(peers)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                elif msg_type == MSG_INFO:
                    try:
                        info = json.loads(payload)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        info = {}
                    err_msg = info.get('error', '')
                    if err_msg:
                        log.error('Server error: %s', err_msg)
                        self._set_status('Rejected')
                        if self._on_error:
                            self._on_error(err_msg)
                        self.running = False
                        break
                    # Ignore informational messages (e.g. assigned_ip after reconnect)
                elif msg_type == MSG_KEEPALIVE:
                    pass  # server pong, ignore
            except (ConnectionError, OSError):
                if not self.running:
                    break
                elapsed = time.monotonic() - self._connected_at
                log.error('Server connection lost after %.1fs', elapsed)
                self._close_socket()
                # If connection dropped within 3s, likely a TLS mismatch
                if elapsed < 3:
                    self._reconnect_count += 1
                    if self._reconnect_count >= 2:
                        tls_hint = (
                            'Connection keeps dropping immediately.\n\n'
                            'The server likely expects TLS but the client is not using it '
                            '(or vice versa).\n\n'
                            'Check the "Use TLS encryption" checkbox matches the server config.'
                        )
                        self._set_status('TLS mismatch?')
                        if self._on_error:
                            self._on_error(tls_hint)
                        self.running = False
                        break
                self._set_status('Connection lost')
                if not self._reconnect():
                    break
            except Exception:
                if self.running:
                    log.exception('Server read error')
                break

    def start(self, host: str, port: int, name: str, use_tls: bool = False,
              preferred_ip: str = '', subnet_pref: str = '') -> str:
        """Connect to server (gets assigned IP), open TAP, start relay threads.

        Returns the assigned virtual IP.
        """
        # Store params for reconnection
        self._host = host
        self._port = port
        self._name = name
        self._use_tls = use_tls
        self._subnet_pref = subnet_pref
        self._reconnect_count = 0

        # Open Wintun adapter. No MAC needed (L3 driver — point-to-point).
        self.tap = WintunAdapter()
        self.tap.open()

        # Connect and get assigned IP from server
        assigned_ip = self.connect(host, port, name, preferred_ip, use_tls,
                                   subnet_pref=subnet_pref)
        self._ip_addr = assigned_ip

        # Configure the IP on the (already-open) Wintun adapter
        self.tap.configure_ip(assigned_ip)

        self.running = True

        t1 = threading.Thread(target=self._tap_to_server, daemon=True, name='tap→srv')
        t2 = threading.Thread(target=self._server_to_tap, daemon=True, name='srv→tap')
        t3 = threading.Thread(target=self._ping_loop, daemon=True, name='ping')
        t4 = threading.Thread(target=self._stats_loop, daemon=True, name='stats')
        t1.start()
        t2.start()
        t3.start()
        t4.start()
        self._threads = [t1, t2, t3, t4]
        return assigned_ip

    def _ping_loop(self):
        """Every 5s, send a PING to every known peer to measure RTT."""
        while self.running:
            time.sleep(5)
            if not self.running:
                break
            for peer in list(self._known_peers):
                peer_ip = peer.get('ip', '')
                if not peer_ip or peer_ip == self._ip_addr:
                    continue
                payload = json.dumps({
                    'from': self._ip_addr,
                    'to': peer_ip,
                    'ts': time.monotonic(),
                }).encode('utf-8')
                try:
                    with self._lock:
                        if self.sock:
                            self.sock.sendall(pack_message(MSG_PING, payload))
                except OSError:
                    pass

    def _stats_loop(self):
        """Log frame counters every 15s for debugging."""
        last_to, last_from = 0, 0
        while self.running:
            time.sleep(15)
            if not self.running:
                break
            sent = self.frames_to_server - last_to
            recv = self.frames_from_server - last_from
            last_to = self.frames_to_server
            last_from = self.frames_from_server
            log.info('Frames last 15s: TAP→srv=%d, srv→TAP=%d (totals: %d / %d)',
                     sent, recv, self.frames_to_server, self.frames_from_server)

    def stop(self):
        """Disconnect and clean up."""
        self.running = False
        # Close socket first so server-bound threads unblock immediately
        self._close_socket()
        if self.tap:
            try:
                self.tap.close()
            except OSError:
                pass
            self.tap = None
        # Threads are daemons; only briefly wait for the I/O ones (not sleep loops)
        for t in self._threads:
            if t.name in ('ping', 'stats'):
                continue  # they sleep; just let daemon kill them
            t.join(timeout=0.5)
        self._threads.clear()
        log.info('Client stopped')


# ---------------------------------------------------------------------------
# Query peers without joining
# ---------------------------------------------------------------------------
def query_peers(host: str, port: int, use_tls: bool = False) -> list[dict]:
    """Open a temporary connection, send MSG_QUERY, get peer list, close."""
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    raw.settimeout(5)
    try:
        if use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw)
        else:
            sock = raw
        sock.connect((host, port))
        sock.sendall(pack_message(MSG_QUERY, b''))
        # Read MSG_PEERS response
        header = b''
        while len(header) < HEADER_SIZE:
            chunk = sock.recv(HEADER_SIZE - len(header))
            if not chunk:
                return []
            header += chunk
        length, msg_type = unpack_header(header)
        payload = b''
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                return []
            payload += chunk
        if msg_type == MSG_PEERS:
            return json.loads(payload)
        return []
    except Exception as e:
        log.warning('query_peers failed: %s', e)
        raise
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _suggest_ip(peers: list[dict]) -> str:
    """Suggest the next available IP in 10.10.0.x range."""
    taken = set()
    for p in peers:
        ip = p.get('ip', '')
        parts = ip.split('.')
        if len(parts) == 4 and parts[0] == '10' and parts[1] == '10' and parts[2] == '0':
            try:
                taken.add(int(parts[3]))
            except ValueError:
                pass
    for i in range(1, 255):
        if i not in taken:
            return f'10.10.0.{i}'
    return '10.10.0.1'


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(cfg: dict):
    try:
        with open(_CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        log.warning('Could not save config to %s', _CONFIG_FILE)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class TunnelGUI:
    # Common LAN-style /24 ranges users can pick from. Display label -> subnet (first 3 octets).
    SUBNET_OPTIONS = [
        ('10.10.0.x   (default)',          '10.10.0'),
        ('192.168.137.x   (Hamachi-style)', '192.168.137'),
        ('192.168.50.x',                    '192.168.50'),
        ('192.168.0.x',                     '192.168.0'),
        ('192.168.1.x',                     '192.168.1'),
        ('25.10.10.x   (Radmin-style)',     '25.10.10'),
    ]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title('LAN Game Tunnel')
        self.root.geometry('420x500')
        self.root.resizable(False, False)

        self.client: TunnelClient | None = None
        self._connected = False
        self._build_ui()
        self._load_saved_config()
        self._start_ping_refresh()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _build_ui(self):
        style = ttk.Style()
        style.configure('Title.TLabel', font=('Segoe UI', 16, 'bold'))
        style.configure('Status.TLabel', font=('Segoe UI', 10))

        main = ttk.Frame(self.root, padding=20)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text='LAN Game Tunnel', style='Title.TLabel').pack(pady=(0, 15))

        # -- Server connection -----------------------------------------------
        srv = ttk.LabelFrame(main, text='Server Connection', padding=10)
        srv.pack(fill=tk.X, pady=5)

        ttk.Label(srv, text='Server:').grid(row=0, column=0, sticky=tk.W, pady=3)
        self.host_var = tk.StringVar(value='127.0.0.1')
        ttk.Entry(srv, textvariable=self.host_var, width=28).grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(srv, text='Port:').grid(row=1, column=0, sticky=tk.W, pady=3)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(srv, textvariable=self.port_var, width=28).grid(row=1, column=1, padx=5, pady=3)

        ttk.Label(srv, text='Name:').grid(row=2, column=0, sticky=tk.W, pady=3)
        self.name_var = tk.StringVar(value='Player')
        ttk.Entry(srv, textvariable=self.name_var, width=28).grid(row=2, column=1, padx=5, pady=3)

        # -- Network settings ------------------------------------------------
        net = ttk.LabelFrame(main, text='Network', padding=10)
        net.pack(fill=tk.X, pady=5)

        ttk.Label(net, text='Range:').grid(row=0, column=0, sticky=tk.W, pady=3)
        self.subnet_label_var = tk.StringVar(value=self.SUBNET_OPTIONS[0][0])
        self.subnet_combo = ttk.Combobox(
            net, textvariable=self.subnet_label_var,
            values=[label for label, _ in self.SUBNET_OPTIONS],
            state='readonly', width=26,
        )
        self.subnet_combo.grid(row=0, column=1, padx=5, pady=3, sticky=tk.W)

        ttk.Label(net, text='Virtual IP:').grid(row=1, column=0, sticky=tk.W, pady=3)
        self.ip_var = tk.StringVar(value='(auto-assigned by server)')
        ttk.Label(net, textvariable=self.ip_var, foreground='#0066cc',
                  font=('Consolas', 10, 'bold')).grid(row=1, column=1, sticky=tk.W, padx=5, pady=3)

        self.tls_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(net, text='Use TLS encryption', variable=self.tls_var).grid(
            row=2, column=0, columnspan=2, sticky=tk.W, pady=3,
        )

        # -- Buttons ---------------------------------------------------------
        btn = ttk.Frame(main)
        btn.pack(fill=tk.X, pady=8)

        self.refresh_btn = ttk.Button(btn, text='Refresh Peers', command=self._on_refresh)
        self.refresh_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=3)

        self.connect_btn = ttk.Button(btn, text='Connect', command=self._on_connect)
        self.connect_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=3)

        self.disconnect_btn = ttk.Button(btn, text='Disconnect', command=self._on_disconnect, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=3)

        # -- Status ----------------------------------------------------------
        self.status_var = tk.StringVar(value='Disconnected')
        ttk.Label(main, textvariable=self.status_var, style='Status.TLabel').pack(pady=(5, 2))

        # -- Peers list ------------------------------------------------------
        peers_frame = ttk.LabelFrame(main, text='Connected Peers', padding=5)
        peers_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.peers_text = tk.Text(peers_frame, height=6, width=45, state=tk.DISABLED,
                                  font=('Consolas', 9), bg='#f5f5f5', relief=tk.FLAT)
        self.peers_text.pack(fill=tk.BOTH, expand=True)
        # Color tags for ping quality
        self.peers_text.tag_configure('good', foreground='#0a8a3a')   # green
        self.peers_text.tag_configure('ok',   foreground='#b58900')   # yellow/amber
        self.peers_text.tag_configure('bad',  foreground='#c0392b')   # red
        self.peers_text.tag_configure('dim',  foreground='#888888')   # gray
        self.peers_text.tag_configure('me',   foreground='#0066cc', font=('Consolas', 9, 'bold'))

    # -- config persistence ------------------------------------------------

    def _load_saved_config(self):
        cfg = _load_config()
        if cfg.get('host'):
            self.host_var.set(cfg['host'])
        if cfg.get('port'):
            self.port_var.set(str(cfg['port']))
        if cfg.get('name'):
            self.name_var.set(cfg['name'])
        if cfg.get('tls') is not None:
            self.tls_var.set(cfg['tls'])
        # Remember last assigned IP as a preference for next connect
        self._preferred_ip = cfg.get('ip', '')
        # Restore subnet selection if saved
        saved_subnet = cfg.get('subnet', '')
        if saved_subnet:
            for label, sub in self.SUBNET_OPTIONS:
                if sub == saved_subnet:
                    self.subnet_label_var.set(label)
                    break

    def _selected_subnet(self) -> str:
        """Return the first 3 octets of the subnet currently picked in the dropdown."""
        label = self.subnet_label_var.get()
        for lbl, sub in self.SUBNET_OPTIONS:
            if lbl == label:
                return sub
        return self.SUBNET_OPTIONS[0][1]

    def _save_current_config(self):
        _save_config({
            'host': self.host_var.get().strip(),
            'port': self.port_var.get().strip(),
            'name': self.name_var.get().strip(),
            'ip': getattr(self, '_preferred_ip', ''),
            'tls': self.tls_var.get(),
            'subnet': self._selected_subnet(),
        })

    # -- peer list display -------------------------------------------------

    def _update_peers(self, peers: list):
        """Update the peers text box (called from any thread)."""
        # Snapshot RTT map from the client (if connected)
        rtt_map = self.client.peer_rtt if self.client else {}
        my_ip = self.client._ip_addr if self.client else ''
        now = time.monotonic()

        def _do():
            self.peers_text.config(state=tk.NORMAL)
            self.peers_text.delete('1.0', tk.END)
            if not peers:
                self.peers_text.insert(tk.END, '  No peers connected\n', 'dim')
            else:
                for p in peers:
                    ip = p.get('ip', '?')
                    name = p.get('name', '?')
                    if ip == my_ip:
                        rtt_str = '   (you)'
                        tag = 'me'
                    else:
                        entry = rtt_map.get(ip)
                        if entry and (now - entry[1]) < 15:
                            ms = entry[0]
                            rtt_str = f'{ms:5.0f}ms'
                            if ms < 150:
                                tag = 'good'
                            elif ms < 300:
                                tag = 'ok'
                            else:
                                tag = 'bad'
                        else:
                            rtt_str = '   ----'
                            tag = 'dim'
                    self.peers_text.insert(tk.END, f'  {ip:15s} {rtt_str}  {name}\n', tag)
            self.peers_text.config(state=tk.DISABLED)
        self.root.after(0, _do)

    def _start_ping_refresh(self):
        """Periodically re-render the peers list so RTT colors stay fresh."""
        def tick():
            if self.client and self._connected:
                self._update_peers(self.client._known_peers or [])
            self.root.after(2000, tick)
        self.root.after(2000, tick)

    # -- actions ------------------------------------------------------------

    def _on_refresh(self):
        """Query the server for connected peers without joining."""
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror('Error', 'Server address is required')
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror('Error', 'Invalid port number')
            return

        self.refresh_btn.config(state=tk.DISABLED)
        self.status_var.set('Querying...')

        def do_query():
            try:
                peers = query_peers(host, port, self.tls_var.get())
                def update():
                    self._update_peers(peers)
                    self.status_var.set(f'{len(peers)} peer(s) online')
                    self.refresh_btn.config(state=tk.NORMAL)
                self.root.after(0, update)
            except Exception as e:
                err = str(e)
                def show_err():
                    self.status_var.set('Query failed')
                    self.refresh_btn.config(state=tk.NORMAL)
                    messagebox.showerror('Error', f'Could not reach server:\n{err}')
                self.root.after(0, show_err)

        threading.Thread(target=do_query, daemon=True).start()

    def _on_connect(self):
        host = self.host_var.get().strip()
        name = self.name_var.get().strip() or 'Player'

        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror('Error', 'Invalid port number')
            return

        if not host:
            messagebox.showerror('Error', 'Server address is required')
            return

        self._save_current_config()
        self.connect_btn.config(state=tk.DISABLED)
        self.status_var.set('Connecting...')
        self.ip_var.set('(requesting from server...)')

        def status_callback(msg: str):
            self.root.after(0, lambda m=msg: self.status_var.set(m))

        def do_connect():
            try:
                self.client = TunnelClient(
                    on_status=status_callback,
                    on_peers=self._update_peers,
                )
                self.client._on_error = lambda msg: self.root.after(
                    0, lambda m=msg: self._on_error(m)
                )
                preferred = getattr(self, '_preferred_ip', '')
                subnet_pref = self._selected_subnet()
                # If user changed subnet, drop the old preferred-IP hint so
                # the server picks a fresh one in the new range.
                if preferred and not preferred.startswith(subnet_pref + '.'):
                    preferred = ''
                assigned = self.client.start(host, port, name,
                                             self.tls_var.get(), preferred,
                                             subnet_pref=subnet_pref)
                self._preferred_ip = assigned
                self._save_current_config()
                self.root.after(0, lambda: self._on_connected(assigned))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda err=err: self._on_error(err))

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self, assigned_ip: str = ''):
        self._connected = True
        self.disconnect_btn.config(state=tk.NORMAL)
        self.subnet_combo.config(state=tk.DISABLED)
        if assigned_ip:
            self.ip_var.set(assigned_ip)
        self.status_var.set(f'Connected to {self.host_var.get()}:{self.port_var.get()}')
        # Hide window to taskbar after a brief moment so user can see we connected
        self.root.after(1500, self._minimize_to_taskbar)

    def _minimize_to_taskbar(self):
        if self._connected:
            try:
                self.root.iconify()
            except tk.TclError:
                pass

    def _on_disconnect(self):
        self._connected = False
        self.disconnect_btn.config(state=tk.DISABLED)
        self.status_var.set('Disconnecting...')

        def do_disconnect():
            if self.client:
                self.client.stop()
                self.client = None
            self.root.after(0, self._reset_ui)

        threading.Thread(target=do_disconnect, daemon=True).start()

    def _on_error(self, msg: str):
        self._connected = False
        messagebox.showerror('Connection Error', msg)
        self._reset_ui()

    def _reset_ui(self):
        self._connected = False
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.subnet_combo.config(state='readonly')
        self.status_var.set('Disconnected')
        self.ip_var.set('(auto-assigned by server)')
        self._update_peers([])

    def _on_close(self):
        """Called when user clicks the window's [X] button."""
        if self._connected:
            answer = messagebox.askyesnocancel(
                'Still connected',
                'You are still connected to the tunnel.\n\n'
                'YES   = Disconnect and quit\n'
                'NO    = Hide window (stay connected in background)\n'
                'CANCEL = Keep window open',
            )
            if answer is None:
                return  # Cancel: do nothing
            if answer is False:
                # Hide to taskbar, keep tunnel running
                self.root.iconify()
                return

        # Either not connected, or user confirmed quit — actually shut down.
        def do_close():
            if self.client:
                self.client.stop()
            self.root.after(0, self.root.destroy)

        threading.Thread(target=do_close, daemon=True).start()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# UAC elevation helper
# ---------------------------------------------------------------------------
def _is_admin() -> bool:
    """Check if running with Administrator privileges on Windows."""
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _request_elevation():
    """Re-launch ourselves as Administrator via UAC prompt."""
    import ctypes
    script = os.path.abspath(sys.argv[0])
    # ShellExecuteW returns >32 on success
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, 'runas', sys.executable, f'"{script}"', None, 1,
    )
    if ret <= 32:
        messagebox.showerror('Error', 'Failed to request Administrator privileges.')
        sys.exit(1)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    if not _is_admin():
        _request_elevation()
    app = TunnelGUI()
    app.run()
