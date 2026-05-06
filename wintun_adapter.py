"""Wintun adapter wrapper (WireGuard's L3 TUN driver for Windows).

Wintun is dramatically simpler than TAP-Windows6:
  - L3 (raw IP packets), no Ethernet header, no MAC, no ARP.
  - Single signed DLL (wintun.dll) — no driver install, no .inf, no NDIS.
  - Point-to-point — packets go in/out by IP destination only.

This is the same approach Hamachi/ZeroTier/WireGuard use. Eliminates
the entire class of "Windows silently dropped my ARP request" failures.

Requires `wintun.dll` (amd64) next to the exe / in sys._MEIPASS.
Download: https://www.wintun.net/  (MIT-licensed, by WireGuard LLC).
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import logging

log = logging.getLogger('wintun-adapter')

# Stable GUID for our adapter so it persists across runs (avoids littering
# Device Manager with new adapters every connect).
# {AB1B2C3D-4E5F-6789-ABCD-EF0123456789}
_ADAPTER_GUID_BYTES = (ctypes.c_ubyte * 16)(
    0xAB, 0x1B, 0x2C, 0x3D, 0x4E, 0x5F, 0x67, 0x89,
    0xAB, 0xCD, 0xEF, 0x01, 0x23, 0x45, 0x67, 0x89,
)

ADAPTER_NAME = 'LAN Game Tunnel'
TUNNEL_TYPE = 'WireGuard'   # arbitrary label shown in Network Connections

# Wintun ring buffer capacity (bytes). Min 0x20000 (128KB), Max 0x4000000 (64MB).
RING_CAPACITY = 0x400000  # 4 MiB

INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_NO_MORE_ITEMS = 259


def _dll_path() -> str:
    """Return absolute path to bundled wintun.dll."""
    base = getattr(sys, '_MEIPASS', None)
    if base and os.path.isfile(os.path.join(base, 'wintun.dll')):
        return os.path.join(base, 'wintun.dll')
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, 'wintun.dll')


# ---------------------------------------------------------------------------
# DLL bindings (lazy-loaded so non-Windows environments can import for tests)
# ---------------------------------------------------------------------------
_dll = None
kernel32 = None


def _load_dll():
    global _dll, kernel32
    if _dll is not None:
        return _dll
    path = _dll_path()
    if not os.path.isfile(path):
        raise RuntimeError(
            f'wintun.dll not found at {path}.\n\n'
            'Download from https://www.wintun.net/ and place wintun.dll '
            'next to the executable.'
        )
    _dll = ctypes.WinDLL(path, use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    GUID_PTR = ctypes.POINTER(ctypes.c_ubyte * 16)

    _dll.WintunCreateAdapter.argtypes = [wt.LPCWSTR, wt.LPCWSTR, GUID_PTR]
    _dll.WintunCreateAdapter.restype = ctypes.c_void_p

    _dll.WintunOpenAdapter.argtypes = [wt.LPCWSTR]
    _dll.WintunOpenAdapter.restype = ctypes.c_void_p

    _dll.WintunCloseAdapter.argtypes = [ctypes.c_void_p]
    _dll.WintunCloseAdapter.restype = ctypes.c_bool

    _dll.WintunStartSession.argtypes = [ctypes.c_void_p, wt.DWORD]
    _dll.WintunStartSession.restype = ctypes.c_void_p

    _dll.WintunEndSession.argtypes = [ctypes.c_void_p]
    _dll.WintunEndSession.restype = None

    _dll.WintunGetReadWaitEvent.argtypes = [ctypes.c_void_p]
    _dll.WintunGetReadWaitEvent.restype = ctypes.c_void_p

    _dll.WintunReceivePacket.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    _dll.WintunReceivePacket.restype = ctypes.c_void_p

    _dll.WintunReleaseReceivePacket.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _dll.WintunReleaseReceivePacket.restype = None

    _dll.WintunAllocateSendPacket.argtypes = [ctypes.c_void_p, wt.DWORD]
    _dll.WintunAllocateSendPacket.restype = ctypes.c_void_p

    _dll.WintunSendPacket.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _dll.WintunSendPacket.restype = None

    _dll.WintunGetAdapterLUID.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
    _dll.WintunGetAdapterLUID.restype = None

    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
    kernel32.WaitForSingleObject.restype = wt.DWORD

    return _dll


# ---------------------------------------------------------------------------
# WintunAdapter
# ---------------------------------------------------------------------------
class WintunAdapter:
    """Thin wrapper that mimics the read()/write()/configure_ip() API of
    the old TAPAdapter so client.py can stay almost identical."""

    def __init__(self):
        self._adapter = None
        self._session = None
        self._read_event = None
        self.name = ADAPTER_NAME  # friendly name in Network Connections

    # -- open / close ---------------------------------------------------
    def open(self):
        dll = _load_dll()

        # Try to open a pre-existing adapter (re-use across runs)
        self._adapter = dll.WintunOpenAdapter(ADAPTER_NAME)
        if not self._adapter:
            log.info('Creating new Wintun adapter "%s"', ADAPTER_NAME)
            self._adapter = dll.WintunCreateAdapter(
                ADAPTER_NAME, TUNNEL_TYPE,
                ctypes.byref(_ADAPTER_GUID_BYTES),
            )
            if not self._adapter:
                err = ctypes.get_last_error()
                raise RuntimeError(
                    f'WintunCreateAdapter failed (Win32 error {err}).\n\n'
                    'Make sure you ran the app as Administrator.'
                )
        else:
            log.info('Opened existing Wintun adapter "%s"', ADAPTER_NAME)

        self._session = dll.WintunStartSession(self._adapter, RING_CAPACITY)
        if not self._session:
            err = ctypes.get_last_error()
            raise RuntimeError(f'WintunStartSession failed (Win32 error {err})')

        self._read_event = dll.WintunGetReadWaitEvent(self._session)
        log.info('Wintun session started (%d KB ring buffer)',
                 RING_CAPACITY // 1024)

    def close(self):
        if not _dll:
            return
        if self._session:
            try:
                _dll.WintunEndSession(self._session)
            except OSError:
                pass
            self._session = None
        if self._adapter:
            try:
                _dll.WintunCloseAdapter(self._adapter)
            except OSError:
                pass
            self._adapter = None
        log.info('Wintun adapter closed')

    # -- I/O ------------------------------------------------------------
    def read(self, _bufsize: int = 0) -> bytes:
        """Read one IPv4/IPv6 packet. Returns b'' on timeout (500ms)."""
        if not self._session:
            return b''
        size = wt.DWORD(0)
        ptr = _dll.WintunReceivePacket(self._session, ctypes.byref(size))
        if ptr:
            data = ctypes.string_at(ptr, size.value)
            _dll.WintunReleaseReceivePacket(self._session, ptr)
            return data
        # No packet available — wait up to 500ms for one
        err = ctypes.get_last_error()
        if err == ERROR_NO_MORE_ITEMS:
            wait = kernel32.WaitForSingleObject(self._read_event, 500)
            if wait != WAIT_OBJECT_0:
                return b''
            # Try once more
            ptr = _dll.WintunReceivePacket(self._session, ctypes.byref(size))
            if ptr:
                data = ctypes.string_at(ptr, size.value)
                _dll.WintunReleaseReceivePacket(self._session, ptr)
                return data
        return b''

    def write(self, data: bytes) -> int:
        """Write one IPv4/IPv6 packet."""
        if not self._session or not data:
            return 0
        n = len(data)
        ptr = _dll.WintunAllocateSendPacket(self._session, n)
        if not ptr:
            # Ring buffer full; drop packet (correct UDP-style behaviour)
            return 0
        ctypes.memmove(ptr, data, n)
        _dll.WintunSendPacket(self._session, ptr)
        return n

    # -- IP / firewall config (uses same netsh approach as TAP) --------
    def configure_ip(self, ip: str, mask: str = '255.255.255.0'):
        """Assign a static IP, force lowest metric (so LAN broadcasts go
        through the tunnel), and add wide-open firewall rules for the subnet."""
        # 1. Wait for the OS to surface the adapter (Wintun publishes it
        #    asynchronously after WintunCreateAdapter).
        for _ in range(20):
            r = subprocess.run(
                ['netsh', 'interface', 'ipv4', 'show', 'interfaces'],
                capture_output=True, timeout=10,
            )
            if ADAPTER_NAME.lower() in (r.stdout or b'').decode(
                    'utf-8', errors='replace').lower():
                break
            import time
            time.sleep(0.25)

        # 2. Set static IP
        cmd = [
            'netsh', 'interface', 'ipv4', 'set', 'address',
            f'name={ADAPTER_NAME}', 'static', ip, mask,
        ]
        log.info('Configuring IP: %s/%s on "%s"', ip, mask, ADAPTER_NAME)
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=15)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b'').decode('utf-8', errors='replace').strip()
            log.warning('Failed to set IP: %s', stderr)
            raise RuntimeError(
                f'Could not set IP on Wintun adapter.\n\n'
                f'Run the app as Administrator.\n\nDetail: {stderr}'
            ) from None

        # 3. Force metric=1 so LAN game broadcasts (255.255.255.255) and
        #    subnet broadcasts (10.10.0.255) route through OUR tunnel
        #    instead of WiFi/Ethernet.
        try:
            subprocess.run(
                ['netsh', 'interface', 'ipv4', 'set', 'interface',
                 ADAPTER_NAME, 'metric=1'],
                capture_output=True, timeout=10,
            )
            log.info('Forced Wintun metric=1 (broadcast goes through tunnel)')
        except (OSError, subprocess.TimeoutExpired):
            pass

        # 4. Firewall: blanket allow on the virtual subnet (no ARP/ICMP
        #    edge-cases possible here — Wintun is L3, no MAC layer).
        self._add_firewall_rules(ip)
        self._set_private_profile()

    def _set_private_profile(self):
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f"Set-NetConnectionProfile -InterfaceAlias '{ADAPTER_NAME}' "
                 f"-NetworkCategory Private -ErrorAction SilentlyContinue"],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _add_firewall_rules(ip: str):
        parts = ip.split('.')
        if len(parts) != 4:
            return
        subnet = f'{parts[0]}.{parts[1]}.{parts[2]}.0/24'
        rule = 'LAN Game Tunnel'
        # Wipe and re-add so changes are clean
        subprocess.run(
            ['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name={rule}'],
            capture_output=True, timeout=10,
        )
        for direction in ('in', 'out'):
            subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
                 f'name={rule}', f'dir={direction}', 'action=allow',
                 'protocol=any', 'profile=any', f'remoteip={subnet}'],
                capture_output=True, timeout=10,
            )
        log.info('Firewall: allow any/any on %s', subnet)
