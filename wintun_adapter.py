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

# Hide all child-process console windows (we are a -noconsole app)
_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0

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

    # -- IP / firewall config -----------------------------------------
    # The adapter GUID in registry/Get-NetAdapter form (8-4-4-4-12 hex with braces).
    # First 3 groups are little-endian, last 2 are big-endian.
    # Bytes: AB 1B 2C 3D 4E 5F 67 89 AB CD EF 01 23 45 67 89
    _ADAPTER_GUID_STR = '{3D2C1BAB-5F4E-8967-ABCD-EF0123456789}'

    def configure_ip(self, ip: str, mask: str = '255.255.255.0'):
        """Assign a static IP, force metric=1 (LAN broadcasts go through the
        tunnel), and add wide-open firewall rules for the subnet.

        Uses PowerShell + Get-NetAdapter (filtered by GUID) instead of netsh,
        because:
          * netsh prints errors to stdout, not stderr (so messages get lost)
          * netsh requires the localized friendly name, which can vary
          * PowerShell's *-NetIPAddress cmdlets work with InterfaceIndex,
            which is unambiguous
        """
        prefix = self._mask_to_prefix(mask)
        guid = self._ADAPTER_GUID_STR

        ps_script = (
            "$ErrorActionPreference = 'Stop';"
            f"$guid = '{guid}';"
            f"$ip = '{ip}';"
            f"$prefix = {prefix};"
            "$adapter = $null;"
            # Wait up to 10s for the adapter to surface in TCP/IP stack
            "for ($i=0; $i -lt 40; $i++) {"
            "  $a = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceGuid -eq $guid };"
            "  if ($a) { $adapter = $a; break }"
            "  Start-Sleep -Milliseconds 250"
            "};"
            "if (-not $adapter) { throw \"Wintun adapter with GUID $guid not found in Get-NetAdapter output. Is wintun.dll loaded?\" };"
            "$idx = $adapter.ifIndex;"
            "$alias = $adapter.Name;"
            # Bring it up
            "try { Enable-NetAdapter -InputObject $adapter -Confirm:$false -ErrorAction SilentlyContinue } catch {};"
            # Wipe ALL existing IPv4 addresses on our interface (clean slate)
            "Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue;"
            # ALSO wipe this exact IP from any OTHER interface (prevents
            # 'object already exists' when Wintun was re-created with a new ifIndex
            # but the OS still has the old IP lingering on the dead interface)
            "Get-NetIPAddress -IPAddress $ip -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue;"
            # And drop any leftover routes on our interface
            "Remove-NetRoute -InterfaceIndex $idx -Confirm:$false -ErrorAction SilentlyContinue | Out-Null;"
            # Assign the static IP — tolerate 'already exists' (race with previous run)
            "try {"
            "  New-NetIPAddress -InterfaceIndex $idx -IPAddress $ip -PrefixLength $prefix -ErrorAction Stop | Out-Null"
            "} catch {"
            # If it failed, check whether it's actually already configured correctly
            "  $existing = Get-NetIPAddress -InterfaceIndex $idx -IPAddress $ip -AddressFamily IPv4 -ErrorAction SilentlyContinue;"
            "  if (-not $existing) { throw }"
            "};"
            # Force lowest metric so LAN broadcasts route through the tunnel
            "Set-NetIPInterface -InterfaceIndex $idx -InterfaceMetric 1 -ErrorAction SilentlyContinue;"
            # Mark Private profile so Windows Firewall is permissive
            "try { Set-NetConnectionProfile -InterfaceIndex $idx -NetworkCategory Private -ErrorAction SilentlyContinue } catch {};"
            "Write-Output \"OK alias=$alias idx=$idx\""
        )

        log.info('Configuring IP: %s/%d on Wintun adapter (GUID %s)',
                 ip, prefix, guid)
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-Command', ps_script],
                capture_output=True, timeout=30,
                creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(
                f'Could not invoke PowerShell to configure Wintun IP: {e}'
            ) from None

        stdout = (r.stdout or b'').decode('utf-8', errors='replace').strip()
        stderr = (r.stderr or b'').decode('utf-8', errors='replace').strip()

        if r.returncode != 0 or not stdout.startswith('OK'):
            detail = (stderr or stdout or '(no output)').strip()
            log.warning('Wintun IP config failed: rc=%s stdout=%r stderr=%r',
                        r.returncode, stdout, stderr)
            raise RuntimeError(
                'Could not set IP on Wintun adapter.\n\n'
                'Make sure the app is running as Administrator and that '
                'wintun.dll is bundled correctly.\n\n'
                f'Detail: {detail}'
            )

        log.info('Wintun configured: %s', stdout)

        # Friendly name might have been auto-renamed by Windows; capture it
        # so diagnostic / firewall code uses the real alias.
        try:
            alias_part = [tok for tok in stdout.split() if tok.startswith('alias=')]
            if alias_part:
                alias = alias_part[0].split('=', 1)[1]
                if alias:
                    self.name = alias
        except (IndexError, ValueError):
            pass

        # Firewall: blanket allow on the virtual subnet
        self._add_firewall_rules(ip)

    @staticmethod
    def _mask_to_prefix(mask: str) -> int:
        try:
            parts = [int(x) for x in mask.split('.')]
            n = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
            return bin(n).count('1')
        except (ValueError, IndexError):
            return 24

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
            capture_output=True, timeout=10, creationflags=_NO_WINDOW,
        )
        for direction in ('in', 'out'):
            subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
                 f'name={rule}', f'dir={direction}', 'action=allow',
                 'protocol=any', 'profile=any', f'remoteip={subnet}'],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW,
            )
        log.info('Firewall: allow any/any on %s', subnet)
