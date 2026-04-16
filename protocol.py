"""Wire protocol for LAN Game Tunnel."""

import struct

# Message types
MSG_DATA = 0x01       # Ethernet frame
MSG_HELLO = 0x02      # Client identification
MSG_INFO = 0x03       # Server info/status
MSG_KEEPALIVE = 0x04  # Keepalive

# Header: 4-byte payload length (big-endian) + 1-byte message type
HEADER_FMT = '!IB'
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 5 bytes

DEFAULT_PORT = 21900
MAX_FRAME_SIZE = 65535


def pack_message(msg_type: int, payload: bytes = b'') -> bytes:
    """Pack a message with header + payload."""
    return struct.pack(HEADER_FMT, len(payload), msg_type) + payload


def unpack_header(data: bytes) -> tuple:
    """Unpack a message header, returns (payload_length, msg_type)."""
    return struct.unpack(HEADER_FMT, data)
