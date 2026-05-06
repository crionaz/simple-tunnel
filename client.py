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
from tap_adapter import TAPAdapter

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
        self.tap: TAPAdapter | None = None
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
        # Per-protocol frame counters (parsed from Ethernet header)
        # Keys: 'arp', 'icmp_echo_req', 'icmp_echo_reply', 'icmp_other',
        #       'tcp', 'udp', 'ipv6', 'other'
        self.proto_to: dict[str, int] = {}
        self.proto_from: dict[str, int] = {}

    def connect(self, host: str, port: int, name: str, preferred_ip: str, use_tls: bool = False) -> str:
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
        hello = json.dumps({'name': name, 'ip': preferred_ip}).encode('utf-8')
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
                                        self._ip_addr, self._use_tls)
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
    def _classify_frame(frame: bytes) -> str:
        """Return short protocol tag for an Ethernet frame."""
        if len(frame) < 14:
            return 'other'
        ethertype = (frame[12] << 8) | frame[13]
        if ethertype == 0x0806:
            return 'arp'
        if ethertype == 0x86DD:
            return 'ipv6'
        if ethertype != 0x0800:
            return 'other'
        # IPv4
        if len(frame) < 14 + 20:
            return 'other'
        ip_proto = frame[14 + 9]
        if ip_proto == 1:  # ICMP
            ihl = (frame[14] & 0x0F) * 4
            icmp_off = 14 + ihl
            if len(frame) > icmp_off:
                t = frame[icmp_off]
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
              preferred_ip: str = '') -> str:
        """Connect to server (gets assigned IP), open TAP, start relay threads.

        Returns the assigned virtual IP.
        """
        # Store params for reconnection
        self._host = host
        self._port = port
        self._name = name
        self._use_tls = use_tls
        self._reconnect_count = 0

        # Connect and get assigned IP from server FIRST
        assigned_ip = self.connect(host, port, name, preferred_ip, use_tls)
        self._ip_addr = assigned_ip

        # Now open TAP with the assigned IP
        self.tap = TAPAdapter()
        self.tap.open()
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
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('LAN Game Tunnel')
        self.root.geometry('420x520')
        self.root.resizable(False, False)

        self.client: TunnelClient | None = None
        self._build_ui()
        self._load_saved_config()
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

        ttk.Label(net, text='Virtual IP:').grid(row=0, column=0, sticky=tk.W, pady=3)
        self.ip_var = tk.StringVar(value='(auto-assigned by server)')
        ttk.Label(net, textvariable=self.ip_var, foreground='#0066cc',
                  font=('Consolas', 10, 'bold')).grid(row=0, column=1, sticky=tk.W, padx=5, pady=3)

        self.tls_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(net, text='Use TLS encryption', variable=self.tls_var).grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=3,
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

        # Diagnostic button (ICMP test)
        self.ping_btn = ttk.Button(main, text='Ping Test (ICMP through tunnel)',
                                   command=self._on_ping_test, state=tk.DISABLED)
        self.ping_btn.pack(fill=tk.X, pady=(0, 3))

        # Auto-fix button: applies progressively more aggressive firewall fixes
        self.autofix_btn = ttk.Button(main, text='Auto-Fix Firewall (run on BOTH peers)',
                                       command=self._on_autofix, state=tk.DISABLED)
        self.autofix_btn.pack(fill=tk.X, pady=(0, 5))

        # -- Status ----------------------------------------------------------
        self.status_var = tk.StringVar(value='Disconnected')
        ttk.Label(main, textvariable=self.status_var, style='Status.TLabel').pack(pady=(5, 2))

        # -- Peers list ------------------------------------------------------
        peers_frame = ttk.LabelFrame(main, text='Connected Peers', padding=5)
        peers_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.peers_text = tk.Text(peers_frame, height=5, width=45, state=tk.DISABLED,
                                  font=('Consolas', 9), bg='#f5f5f5', relief=tk.FLAT)
        self.peers_text.pack(fill=tk.BOTH, expand=True)

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

    def _save_current_config(self):
        _save_config({
            'host': self.host_var.get().strip(),
            'port': self.port_var.get().strip(),
            'name': self.name_var.get().strip(),
            'ip': getattr(self, '_preferred_ip', ''),
            'tls': self.tls_var.get(),
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
                self.peers_text.insert(tk.END, '  No peers connected')
            else:
                for p in peers:
                    ip = p.get('ip', '?')
                    name = p.get('name', '?')
                    if ip == my_ip:
                        rtt_str = '   (you)'
                    else:
                        entry = rtt_map.get(ip)
                        if entry and (now - entry[1]) < 15:
                            rtt_str = f'{entry[0]:5.0f}ms'
                        else:
                            rtt_str = '   ----'
                    self.peers_text.insert(tk.END, f'  {ip:13s} {rtt_str}  {name}\n')
            self.peers_text.config(state=tk.DISABLED)
        self.root.after(0, _do)

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
                assigned = self.client.start(host, port, name,
                                             self.tls_var.get(), preferred)
                self._preferred_ip = assigned
                self._save_current_config()
                self.root.after(0, lambda: self._on_connected(assigned))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda err=err: self._on_error(err))

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self, assigned_ip: str = ''):
        self.disconnect_btn.config(state=tk.NORMAL)
        self.ping_btn.config(state=tk.NORMAL)
        self.autofix_btn.config(state=tk.NORMAL)
        if assigned_ip:
            self.ip_var.set(assigned_ip)
        self.status_var.set(f'Connected to {self.host_var.get()}:{self.port_var.get()}')

    def _on_disconnect(self):
        self.disconnect_btn.config(state=tk.DISABLED)
        self.status_var.set('Disconnecting...')

        def do_disconnect():
            if self.client:
                self.client.stop()
                self.client = None
            self.root.after(0, self._reset_ui)

        threading.Thread(target=do_disconnect, daemon=True).start()

    def _on_error(self, msg: str):
        messagebox.showerror('Connection Error', msg)
        self._reset_ui()

    def _reset_ui(self):
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.ping_btn.config(state=tk.DISABLED)
        self.autofix_btn.config(state=tk.DISABLED)
        self.status_var.set('Disconnected')
        self.ip_var.set('(auto-assigned by server)')
        self._update_peers([])

    def _on_ping_test(self):
        """Run a real ICMP ping + comprehensive diagnostics."""
        if not self.client or not self.client._known_peers:
            messagebox.showinfo('Ping Test', 'No peers to ping yet.')
            return
        peers = [p for p in self.client._known_peers
                 if p.get('ip') and p.get('ip') != self.client._ip_addr]
        if not peers:
            messagebox.showinfo('Ping Test', 'No other peers connected.')
            return

        my_ip = self.client._ip_addr
        tap_name = self.client.tap.name if self.client.tap else ''
        self.ping_btn.config(state=tk.DISABLED, text='Pinging + diagnostics...')

        def do_ping():
            import subprocess
            cflags = 0x08000000 if sys.platform == 'win32' else 0  # CREATE_NO_WINDOW

            def run(cmd, timeout=10):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=timeout, creationflags=cflags)
                    return (r.stdout or '') + (('\n[stderr] ' + r.stderr) if r.stderr else '')
                except Exception as e:
                    return f'<error: {e}>'

            sections = []
            sections.append(f'=== My virtual IP: {my_ip}    TAP: {tap_name} ===')

            # Snapshot counters before
            f_to_0 = self.client.frames_to_server
            f_from_0 = self.client.frames_from_server
            proto_to_0 = dict(self.client.proto_to)
            proto_from_0 = dict(self.client.proto_from)

            # ICMP ping each peer (with -S to force source = our virtual IP)
            sections.append('\n=== ICMP PING (raw output) ===')
            short_results = []
            for p in peers:
                ip = p['ip']
                name = p.get('name', '?')
                cmd = ['ping', '-n', '4', '-w', '2000']
                if my_ip:
                    cmd += ['-S', my_ip]
                cmd.append(ip)
                out = run(cmd, timeout=20)
                sections.append(f'\n--- ping {ip} ({name}) ---\n{out}')
                if 'Reply from ' + ip in out or f'bytes from {ip}' in out:
                    short_results.append(f'OK  {ip} ({name})')
                elif 'Destination host unreachable' in out or 'unreachable' in out.lower():
                    short_results.append(f'X   {ip} ({name})  unreachable (no route/ARP)')
                elif 'timed out' in out.lower():
                    short_results.append(f'X   {ip} ({name})  timed out (firewall/relay)')
                else:
                    short_results.append(f'X   {ip} ({name})  failed')

            # Frame counter delta during the ping
            f_to_d = self.client.frames_to_server - f_to_0
            f_from_d = self.client.frames_from_server - f_from_0

            def proto_delta(now: dict, before: dict) -> str:
                keys = set(now) | set(before)
                parts = []
                for k in sorted(keys):
                    d = now.get(k, 0) - before.get(k, 0)
                    if d:
                        parts.append(f'{k}={d}')
                return ', '.join(parts) if parts else '(none)'

            ptd = proto_delta(self.client.proto_to, proto_to_0)
            pfd = proto_delta(self.client.proto_from, proto_from_0)

            # Definitive interpretation based on per-protocol counters
            sent_echo_req = (self.client.proto_to.get('icmp_echo_req', 0)
                             - proto_to_0.get('icmp_echo_req', 0))
            recv_echo_req = (self.client.proto_from.get('icmp_echo_req', 0)
                             - proto_from_0.get('icmp_echo_req', 0))
            recv_echo_reply = (self.client.proto_from.get('icmp_echo_reply', 0)
                               - proto_from_0.get('icmp_echo_reply', 0))

            verdict = ''
            if sent_echo_req == 0:
                verdict = ('!! ROOT CAUSE: Windows is not transmitting ICMP echo to TAP.\n'
                           '   Check route table, ensure ping uses -S <virtual-ip>, or TAP '
                           'media-status is disconnected.')
            elif recv_echo_reply > 0:
                verdict = ('OK at tunnel level: %d replies came back. If ping still says '
                           'timed out, OUR Windows is dropping the reply (very rare).' % recv_echo_reply)
            elif sent_echo_req > 0 and recv_echo_req == 0:
                verdict = ('!! ROOT CAUSE: Echo requests reached the relay (we sent %d) but '
                           'no echo replies came back through the tunnel.\n'
                           '   This means the OTHER peer\'s Windows received the echo but '
                           'silently dropped it (firewall/stealth-mode), so it never '
                           'generated a reply.\n'
                           '   Fix: on the OTHER peer, run as admin:\n'
                           '     netsh advfirewall firewall add rule name="AllowAllICMPv4" '
                           'protocol=icmpv4:any,any dir=in action=allow profile=any\n'
                           '   Or click "Auto-Fix Firewall" in this app on the OTHER peer.'
                           % sent_echo_req)
            else:
                verdict = ('Mixed: sent %d echo requests, received %d echo requests from peer, '
                           '%d echo replies. Inconclusive.'
                           % (sent_echo_req, recv_echo_req, recv_echo_reply))

            sections.append(
                f'\n=== Tunnel frame activity during ping ===\n'
                f'  TAP -> server: {f_to_d} frames total ({ptd})\n'
                f'  server -> TAP: {f_from_d} frames total ({pfd})\n'
                f'\n  VERDICT:\n  {verdict}'
            )

            # ARP table - did we learn the peer's MAC?
            sections.append('\n=== ARP table for virtual subnet ===')
            arp = run(['arp', '-a'])
            for line in arp.splitlines():
                if '10.10.0.' in line or 'Interface:' in line:
                    sections.append('  ' + line.strip())

            # Route table for our subnet
            sections.append('\n=== Routes for 10.10.0.0/24 ===')
            routes = run(['route', 'print', '10.10.0.*'])
            for line in routes.splitlines():
                if '10.10.0' in line or 'Network Destination' in line:
                    sections.append('  ' + line.strip())

            # TAP adapter status
            sections.append('\n=== TAP adapter status ===')
            if tap_name:
                sections.append(run(
                    ['netsh', 'interface', 'ip', 'show', 'addresses',
                     f'name={tap_name}']))

            # Network profile of TAP
            sections.append('\n=== Network profile (must be Private) ===')
            sections.append(run(['powershell', '-NoProfile', '-Command',
                                  'Get-NetConnectionProfile | Format-List Name,InterfaceAlias,NetworkCategory']))

            # Firewall global state
            sections.append('\n=== Windows Firewall profile state ===')
            sections.append(run(['netsh', 'advfirewall', 'show', 'allprofiles', 'state']))

            # Our firewall rules
            sections.append('\n=== Our firewall rules (LAN Game Tunnel*) ===')
            sections.append(run(['netsh', 'advfirewall', 'firewall', 'show', 'rule',
                                  'name=LAN Game Tunnel']))
            sections.append(run(['netsh', 'advfirewall', 'firewall', 'show', 'rule',
                                  'name=LAN Game Tunnel ICMP']))

            # Built-in ICMP echo rule status
            sections.append('\n=== Built-in ICMPv4 Echo Request rule ===')
            sections.append(run(['powershell', '-NoProfile', '-Command',
                                  '(Get-NetFirewallRule -DisplayName "*Echo Request - ICMPv4-In*" '
                                  '-ErrorAction SilentlyContinue) | Format-Table DisplayName,Enabled,Profile,Action -AutoSize | Out-String']))

            full_report = '\n'.join(sections)

            # Also log to console for the user / for us to debug
            log.info('===== PING DIAGNOSTICS =====\n%s\n===== END =====', full_report)

            # Try to save to a file next to the executable for easy sharing
            try:
                import tempfile, os, time as _t
                report_path = os.path.join(tempfile.gettempdir(),
                                            f'lan-tunnel-diag-{int(_t.time())}.txt')
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(full_report)
            except OSError:
                report_path = ''

            summary = ('Ping summary:\n  ' + '\n  '.join(short_results) +
                       f'\n\nFull diagnostic report saved to:\n  {report_path}\n\n'
                       'Click "Show details" / scroll the log window in the app for full output.\n'
                       'Send the diagnostic file if you need help.')

            def show():
                self.ping_btn.config(state=tk.NORMAL, text='Ping Test (ICMP through tunnel)')
                # Show short summary in messagebox + open the diag file
                messagebox.showinfo('Ping Test', summary)
                if report_path and sys.platform == 'win32':
                    try:
                        os.startfile(report_path)  # opens in Notepad
                    except OSError:
                        pass
            self.root.after(0, show)

        threading.Thread(target=do_ping, daemon=True).start()

    def _on_autofix(self):
        """Apply progressively aggressive firewall fixes, retest after each step."""
        if not self.client or not self.client.tap:
            messagebox.showinfo('Auto-Fix', 'Connect first.')
            return
        peers = [p for p in (self.client._known_peers or [])
                 if p.get('ip') and p.get('ip') != self.client._ip_addr]
        if not peers:
            messagebox.showinfo('Auto-Fix', 'No other peers to ping.')
            return

        target_ip = peers[0]['ip']
        my_ip = self.client._ip_addr
        tap_name = self.client.tap.name
        self.autofix_btn.config(state=tk.DISABLED, text='Auto-fixing...')

        def worker():
            import subprocess
            cflags = 0x08000000 if sys.platform == 'win32' else 0

            def sh(cmd):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=15, creationflags=cflags)
                    return r.returncode == 0, (r.stdout or '') + (r.stderr or '')
                except Exception as e:
                    return False, str(e)

            def can_ping():
                ok, out = sh(['ping', '-n', '2', '-w', '1500', '-S', my_ip, target_ip])
                return 'Reply from ' + target_ip in out

            log_lines = []

            def step(label, *cmds):
                log_lines.append(f'\n[STEP] {label}')
                for c in cmds:
                    ok, out = sh(c)
                    log_lines.append(f'  $ {" ".join(c)}\n  -> {"OK" if ok else "FAIL"}: '
                                     f'{out.strip()[:200]}')
                time.sleep(2)
                worked = can_ping()
                log_lines.append(f'  ping after step: {"SUCCESS" if worked else "still failing"}')
                return worked

            # Step 0: baseline
            log_lines.append(f'Baseline ping {my_ip} -> {target_ip}: '
                             f'{"OK" if can_ping() else "fail"}')

            steps = [
                ('Re-set TAP profile to Private', [
                    'powershell', '-NoProfile', '-Command',
                    f"Set-NetConnectionProfile -InterfaceAlias '{tap_name}' "
                    f"-NetworkCategory Private -ErrorAction SilentlyContinue"
                ]),
                ('Add wide-open ICMP allow (no remoteip, no profile)', [
                    'netsh', 'advfirewall', 'firewall', 'delete', 'rule',
                    'name=AllowAllICMPv4',
                ]),
                ('Add wide-open ICMP allow (no remoteip, no profile) - add', [
                    'netsh', 'advfirewall', 'firewall', 'add', 'rule',
                    'name=AllowAllICMPv4',
                    'protocol=icmpv4:any,any', 'dir=in', 'action=allow',
                    'profile=any', 'edge=yes',
                ]),
                ('Enable all built-in ICMP echo rules (group)', [
                    'netsh', 'advfirewall', 'firewall', 'set', 'rule',
                    'group=File and Printer Sharing', 'new', 'enable=Yes',
                ]),
                ('Disable Public profile firewall (last resort)', [
                    'netsh', 'advfirewall', 'set', 'publicprofile', 'state', 'off',
                ]),
                ('Disable Private profile firewall (nuclear)', [
                    'netsh', 'advfirewall', 'set', 'privateprofile', 'state', 'off',
                ]),
            ]

            success_at = None
            for i, (label, cmd) in enumerate(steps, 1):
                if step(f'{i}. {label}', cmd):
                    success_at = label
                    break

            full_log = '\n'.join(log_lines)
            log.info('===== AUTO-FIX LOG =====\n%s\n=====', full_log)

            if success_at:
                msg = (f'SUCCESS at step: {success_at}\n\n'
                       f'Ping {my_ip} -> {target_ip} now works!\n\n'
                       'Note: if the successful step disabled a firewall profile, '
                       're-enable it from Windows Security after your gaming session:\n'
                       '  netsh advfirewall set allprofiles state on')
            else:
                msg = (f'FAILED. Even with firewall disabled, ping still does not work.\n\n'
                       'This means the issue is NOT firewall on this machine. Possible causes:\n'
                       '  1. The OTHER peer\'s firewall is blocking (run Auto-Fix on it too)\n'
                       '  2. AV / endpoint protection silently dropping ICMP\n'
                       '  3. TAP driver issue (try reinstalling TAP-Windows)\n\n'
                       'Re-enable firewall:\n  netsh advfirewall set allprofiles state on\n\n'
                       f'Full log:\n{full_log}')

            def show():
                self.autofix_btn.config(state=tk.NORMAL,
                                         text='Auto-Fix Firewall (run on BOTH peers)')
                messagebox.showinfo('Auto-Fix Result', msg)
            self.root.after(0, show)

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
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
