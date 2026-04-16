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
    MSG_DATA, MSG_HELLO, MSG_KEEPALIVE, MSG_PEERS,
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

    def connect(self, host: str, port: int, name: str, ip_addr: str, use_tls: bool = False):
        """Connect to the relay server."""
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        raw.settimeout(5)

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

        self.sock.settimeout(None)
        hello = json.dumps({'name': name, 'ip': ip_addr}).encode('utf-8')
        self.sock.sendall(pack_message(MSG_HELLO, hello))
        log.info('Connected to %s:%d', host, port)

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
                self.connect(self._host, self._port, self._name, self._ip_addr, self._use_tls)
                self._reconnect_count = 0
                self._set_status(f'Reconnected to {self._host}:{self._port}')
                return True
            except Exception as e:
                self._reconnect_count += 1
                log.warning('Reconnect attempt failed: %s', e)
        return False

    def _tap_to_server(self):
        """Read Ethernet frames from TAP and send to server."""
        while self.running:
            try:
                frame = self.tap.read()
                if frame and self.running:
                    with self._lock:
                        if self.sock:
                            self.sock.sendall(pack_message(MSG_DATA, frame))
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
                    self.tap.write(payload)
                elif msg_type == MSG_PEERS:
                    try:
                        peers = json.loads(payload)
                        if self._on_peers:
                            self._on_peers(peers)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                elif msg_type == MSG_KEEPALIVE:
                    pass  # server pong, ignore
            except (ConnectionError, OSError):
                if not self.running:
                    break
                log.error('Server connection lost')
                self._set_status('Connection lost')
                self._close_socket()
                if not self._reconnect():
                    break
            except Exception:
                if self.running:
                    log.exception('Server read error')
                break

    def start(self, host: str, port: int, name: str,
              ip_addr: str, use_tls: bool = False):
        """Open TAP adapter, connect to server, start relay threads."""
        # Store params for reconnection
        self._host = host
        self._port = port
        self._name = name
        self._ip_addr = ip_addr
        self._use_tls = use_tls
        self._reconnect_count = 0

        self.tap = TAPAdapter()
        self.tap.open()
        self.tap.configure_ip(ip_addr)

        self.connect(host, port, name, ip_addr, use_tls)
        self.running = True

        t1 = threading.Thread(target=self._tap_to_server, daemon=True, name='tap→srv')
        t2 = threading.Thread(target=self._server_to_tap, daemon=True, name='srv→tap')
        t1.start()
        t2.start()
        self._threads = [t1, t2]

    def stop(self):
        """Disconnect and clean up."""
        self.running = False
        self._close_socket()
        if self.tap:
            try:
                self.tap.close()
            except OSError:
                pass
            self.tap = None
        for t in self._threads:
            t.join(timeout=0.5)
        self._threads.clear()
        log.info('Client stopped')


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
        self.ip_var = tk.StringVar(value='10.10.0.1')
        ttk.Entry(net, textvariable=self.ip_var, width=28).grid(row=0, column=1, padx=5, pady=3)

        self.tls_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(net, text='Use TLS encryption', variable=self.tls_var).grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=3,
        )

        # -- Buttons ---------------------------------------------------------
        btn = ttk.Frame(main)
        btn.pack(fill=tk.X, pady=8)

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
        if cfg.get('ip'):
            self.ip_var.set(cfg['ip'])
        if cfg.get('tls') is not None:
            self.tls_var.set(cfg['tls'])

    def _save_current_config(self):
        _save_config({
            'host': self.host_var.get().strip(),
            'port': self.port_var.get().strip(),
            'name': self.name_var.get().strip(),
            'ip': self.ip_var.get().strip(),
            'tls': self.tls_var.get(),
        })

    # -- peer list display -------------------------------------------------

    def _update_peers(self, peers: list):
        """Update the peers text box (called from any thread)."""
        def _do():
            self.peers_text.config(state=tk.NORMAL)
            self.peers_text.delete('1.0', tk.END)
            if not peers:
                self.peers_text.insert(tk.END, '  No peers connected')
            else:
                for p in peers:
                    ip = p.get('ip', '?')
                    name = p.get('name', '?')
                    self.peers_text.insert(tk.END, f'  {ip:16s} {name}\n')
            self.peers_text.config(state=tk.DISABLED)
        self.root.after(0, _do)

    # -- actions ------------------------------------------------------------

    def _on_connect(self):
        host = self.host_var.get().strip()
        name = self.name_var.get().strip() or 'Player'
        ip_addr = self.ip_var.get().strip()

        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror('Error', 'Invalid port number')
            return

        if not host:
            messagebox.showerror('Error', 'Server address is required')
            return
        if not ip_addr:
            messagebox.showerror('Error', 'Virtual IP is required')
            return

        self._save_current_config()
        self.connect_btn.config(state=tk.DISABLED)
        self.status_var.set('Connecting...')

        def status_callback(msg: str):
            self.root.after(0, lambda m=msg: self.status_var.set(m))

        def do_connect():
            try:
                self.client = TunnelClient(
                    on_status=status_callback,
                    on_peers=self._update_peers,
                )
                self.client.start(host, port, name, ip_addr, self.tls_var.get())
                self.root.after(0, self._on_connected)
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda err=err: self._on_error(err))

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self):
        self.disconnect_btn.config(state=tk.NORMAL)
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
        self.status_var.set('Disconnected')
        self._update_peers([])

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
