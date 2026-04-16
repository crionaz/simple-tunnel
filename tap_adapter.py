"""Windows TAP adapter interface using ctypes.

Requires TAP-Windows driver (included with OpenVPN).
Download from: https://build.openvpn.net/downloads/releases/
"""

import ctypes
import ctypes.wintypes
import subprocess
import logging

log = logging.getLogger('tap-adapter')

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_SYSTEM = 0x00000004
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

# TAP-Windows IOCTL codes
# CTL_CODE(FILE_DEVICE_UNKNOWN=0x22, function, METHOD_BUFFERED=0, FILE_ANY_ACCESS=0)
TAP_IOCTL_SET_MEDIA_STATUS = (0x22 << 16) | (6 << 2)  # 0x220018

# Registry paths
ADAPTER_KEY = r'SYSTEM\CurrentControlSet\Control\Class\{4D36E972-E325-11CE-BFC1-08002BE10318}'
NETWORK_KEY = r'SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}'

# Device path prefix
USERMODEDEVICEDIR = r'\\.\Global\\'
TAP_WIN_SUFFIX = '.tap'

# ---------------------------------------------------------------------------
# ctypes setup
# ---------------------------------------------------------------------------
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ('Internal', ctypes.c_size_t),
        ('InternalHigh', ctypes.c_size_t),
        ('Offset', ctypes.c_uint32),
        ('OffsetHigh', ctypes.c_uint32),
        ('hEvent', ctypes.c_void_p),
    ]


def _check_handle(result, func, args):
    if result == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


# CreateFileW
kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.errcheck = _check_handle

# DeviceIoControl
kernel32.DeviceIoControl.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]
kernel32.DeviceIoControl.restype = ctypes.c_bool

# ReadFile / WriteFile
kernel32.ReadFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(OVERLAPPED),
]
kernel32.ReadFile.restype = ctypes.c_bool

kernel32.WriteFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(OVERLAPPED),
]
kernel32.WriteFile.restype = ctypes.c_bool

# Event / Wait
kernel32.CreateEventW.argtypes = [
    ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p,
]
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.WaitForSingleObject.restype = ctypes.c_uint32

kernel32.GetOverlappedResult.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(OVERLAPPED),
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_bool,
]
kernel32.GetOverlappedResult.restype = ctypes.c_bool

kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = ctypes.c_bool

kernel32.CancelIo.argtypes = [ctypes.c_void_p]
kernel32.CancelIo.restype = ctypes.c_bool


# ---------------------------------------------------------------------------
# TAP Adapter class
# ---------------------------------------------------------------------------
class TAPAdapter:
    """Interface to a TAP-Windows virtual network adapter."""

    def __init__(self):
        self.handle = None
        self.guid = None
        self.name = None
        self._read_event = None
        self._write_event = None

    # -- discovery ----------------------------------------------------------

    @staticmethod
    def find_adapters() -> list[dict]:
        """Find installed TAP-Windows adapters by scanning the registry."""
        import winreg
        adapters = []
        try:
            reg = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ADAPTER_KEY)
        except OSError:
            return adapters

        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(reg, i)
            except OSError:
                break
            i += 1
            try:
                subkey = winreg.OpenKey(reg, subkey_name)
                comp_id, _ = winreg.QueryValueEx(subkey, 'ComponentId')
                if 'tap' in comp_id.lower():
                    guid, _ = winreg.QueryValueEx(subkey, 'NetCfgInstanceId')
                    name = TAPAdapter._get_adapter_name(guid)
                    adapters.append({'guid': guid, 'name': name, 'component_id': comp_id})
                winreg.CloseKey(subkey)
            except OSError:
                continue
        winreg.CloseKey(reg)
        return adapters

    @staticmethod
    def _get_adapter_name(guid: str) -> str:
        """Get the friendly name of an adapter from its GUID."""
        import winreg
        try:
            path = rf'{NETWORK_KEY}\{guid}\Connection'
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
            name, _ = winreg.QueryValueEx(key, 'Name')
            winreg.CloseKey(key)
            return name
        except OSError:
            return guid

    # -- open / close -------------------------------------------------------

    def open(self, guid: str = None):
        """Open a TAP adapter. Auto-detects if guid is not specified."""
        if guid is None:
            adapters = self.find_adapters()
            if not adapters:
                raise RuntimeError(
                    'No TAP adapter found.\n'
                    'Install TAP-Windows driver from https://build.openvpn.net/downloads/releases/'
                )
            guid = adapters[0]['guid']
            self.name = adapters[0]['name']
            log.info('Using TAP adapter: %s (%s)', self.name, guid)

        self.guid = guid
        device_path = f'{USERMODEDEVICEDIR}{guid}{TAP_WIN_SUFFIX}'

        self.handle = kernel32.CreateFileW(
            device_path,
            GENERIC_READ | GENERIC_WRITE,
            0, None, OPEN_EXISTING,
            FILE_ATTRIBUTE_SYSTEM, None,
        )

        # Create persistent events for overlapped I/O
        self._read_event = kernel32.CreateEventW(None, True, False, None)
        self._write_event = kernel32.CreateEventW(None, True, False, None)

        # Set adapter to "connected" state
        status = ctypes.c_uint32(1)
        out = ctypes.c_uint32(0)
        kernel32.DeviceIoControl(
            self.handle, TAP_IOCTL_SET_MEDIA_STATUS,
            ctypes.byref(status), ctypes.sizeof(status),
            None, 0, ctypes.byref(out), None,
        )
        log.info('TAP adapter opened')

    def close(self):
        """Close the TAP adapter."""
        if self._read_event:
            kernel32.CloseHandle(self._read_event)
            self._read_event = None
        if self._write_event:
            kernel32.CloseHandle(self._write_event)
            self._write_event = None
        if self.handle:
            kernel32.CancelIo(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None
            log.info('TAP adapter closed')

    # -- I/O ----------------------------------------------------------------

    def read(self, bufsize: int = 2048) -> bytes:
        """Read an Ethernet frame (blocking with 2s timeout)."""
        buf = ctypes.create_string_buffer(bufsize)
        n = ctypes.c_uint32(0)
        ovlp = OVERLAPPED()
        ovlp.hEvent = self._read_event

        ret = kernel32.ReadFile(self.handle, buf, bufsize, ctypes.byref(n), ctypes.byref(ovlp))
        if not ret:
            err = ctypes.get_last_error()
            if err == ERROR_IO_PENDING:
                wait = kernel32.WaitForSingleObject(ovlp.hEvent, 2000)
                if wait != WAIT_OBJECT_0:
                    kernel32.CancelIo(self.handle)
                    return b''
                kernel32.GetOverlappedResult(self.handle, ctypes.byref(ovlp), ctypes.byref(n), False)
            else:
                raise OSError(f'TAP ReadFile failed (error {err})')

        return buf.raw[:n.value]

    def write(self, data: bytes) -> int:
        """Write an Ethernet frame."""
        buf = ctypes.create_string_buffer(data)
        n = ctypes.c_uint32(0)
        ovlp = OVERLAPPED()
        ovlp.hEvent = self._write_event

        ret = kernel32.WriteFile(self.handle, buf, len(data), ctypes.byref(n), ctypes.byref(ovlp))
        if not ret:
            err = ctypes.get_last_error()
            if err == ERROR_IO_PENDING:
                kernel32.WaitForSingleObject(ovlp.hEvent, 2000)
                kernel32.GetOverlappedResult(self.handle, ctypes.byref(ovlp), ctypes.byref(n), False)
            else:
                raise OSError(f'TAP WriteFile failed (error {err})')

        return n.value

    # -- IP config ----------------------------------------------------------

    def configure_ip(self, ip: str, mask: str = '255.255.255.0'):
        """Configure the adapter IP address using netsh."""
        if not self.name:
            log.warning('Adapter name unknown, skipping IP configuration')
            return
        cmd = [
            'netsh', 'interface', 'ip', 'set', 'address',
            self.name, 'static', ip, mask,
        ]
        log.info('Configuring IP: %s/%s on "%s"', ip, mask, self.name)
        subprocess.run(cmd, check=True, capture_output=True)
