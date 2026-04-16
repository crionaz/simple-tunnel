"""LAN Game Tunnel - Relay Server.

Accepts connections from multiple clients and broadcasts
Ethernet frames between them, creating a virtual LAN.
"""

import asyncio
import json
import ssl
import logging
import argparse

from protocol import (
    HEADER_SIZE, DEFAULT_PORT, MAX_FRAME_SIZE,
    MSG_DATA, MSG_HELLO, MSG_KEEPALIVE, MSG_PEERS,
    pack_message, unpack_header,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('tunnel-server')


# ---------------------------------------------------------------------------
# Stream helpers for TLS-over-BIO and prefixed reads
# ---------------------------------------------------------------------------
class _PrefixedReader:
    """Wraps an asyncio.StreamReader with bytes already consumed from it."""

    def __init__(self, prefix: bytes, reader: asyncio.StreamReader):
        self._prefix = prefix
        self._reader = reader

    async def readexactly(self, n: int) -> bytes:
        if self._prefix:
            if len(self._prefix) >= n:
                result = self._prefix[:n]
                self._prefix = self._prefix[n:]
                return result
            result = self._prefix
            self._prefix = b''
            result += await self._reader.readexactly(n - len(result))
            return result
        return await self._reader.readexactly(n)


class _TLSReader:
    """Async reader that decrypts via ssl.MemoryBIO."""

    def __init__(self, sslobj, incoming: ssl.MemoryBIO, raw_reader: asyncio.StreamReader):
        self._ssl = sslobj
        self._incoming = incoming
        self._raw = raw_reader

    async def readexactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._ssl.read(n - len(buf))
                if not chunk:
                    raise asyncio.IncompleteReadError(bytes(buf), n)
                buf.extend(chunk)
            except ssl.SSLWantReadError:
                data = await self._raw.read(16384)
                if not data:
                    raise asyncio.IncompleteReadError(bytes(buf), n)
                self._incoming.write(data)
        return bytes(buf)


class _TLSWriter:
    """Async writer that encrypts via ssl.MemoryBIO."""

    def __init__(self, sslobj, outgoing: ssl.MemoryBIO, raw_writer: asyncio.StreamWriter):
        self._ssl = sslobj
        self._outgoing = outgoing
        self._raw = raw_writer

    def get_extra_info(self, key, default=None):
        return self._raw.get_extra_info(key, default)

    def write(self, data: bytes):
        self._ssl.write(data)
        out = self._outgoing.read()
        if out:
            self._raw.write(out)

    async def drain(self):
        await self._raw.drain()

    def close(self):
        self._raw.close()

    async def wait_closed(self):
        await self._raw.wait_closed()


class TunnelServer:
    def __init__(self, host: str = '0.0.0.0', port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.clients: dict[int, asyncio.StreamWriter] = {}
        self.client_names: dict[int, str] = {}
        self.client_ips: dict[int, str] = {}  # virtual IPs

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info('peername')
        cid = id(writer)
        self.clients[cid] = writer
        self.client_names[cid] = str(addr)
        log.info('Client connected: %s', addr)

        try:
            while True:
                header = await reader.readexactly(HEADER_SIZE)
                length, msg_type = unpack_header(header)

                if length > MAX_FRAME_SIZE:
                    log.warning('Oversized message from %s, dropping', addr)
                    await reader.readexactly(length)
                    continue

                payload = await reader.readexactly(length) if length > 0 else b''

                if msg_type == MSG_DATA:
                    await self._broadcast(cid, payload)
                elif msg_type == MSG_HELLO:
                    # Support both legacy plain-text and new JSON format
                    try:
                        info = json.loads(payload)
                        name = info.get('name', str(addr))
                        vip = info.get('ip', '')
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        name = payload.decode('utf-8', errors='replace')
                        vip = ''
                    self.client_names[cid] = name
                    if vip:
                        self.client_ips[cid] = vip
                    log.info('Client %s identified as "%s" (IP: %s)', addr, name, vip or 'unknown')
                    await self._broadcast_peers()
                elif msg_type == MSG_KEEPALIVE:
                    writer.write(pack_message(MSG_KEEPALIVE))
                    await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.clients.pop(cid, None)
            self.client_names.pop(cid, None)
            self.client_ips.pop(cid, None)
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            log.info('Client disconnected: %s', addr)
            await self._broadcast_peers()

    async def _broadcast(self, sender_id: int, frame: bytes):
        """Send an Ethernet frame to all clients except the sender."""
        msg = pack_message(MSG_DATA, frame)
        dead = []
        for cid, writer in self.clients.items():
            if cid == sender_id:
                continue
            try:
                writer.write(msg)
                await writer.drain()
            except (ConnectionError, OSError):
                dead.append(cid)
        for cid in dead:
            self.clients.pop(cid, None)
            self.client_names.pop(cid, None)

    async def _broadcast_peers(self):
        """Send current peer list to all clients."""
        peers = []
        for cid in self.clients:
            peers.append({
                'name': self.client_names.get(cid, '?'),
                'ip': self.client_ips.get(cid, ''),
            })
        log.info('Connected peers: %d', len(peers))
        msg = pack_message(MSG_PEERS, json.dumps(peers).encode('utf-8'))
        dead = []
        for cid, writer in self.clients.items():
            try:
                writer.write(msg)
                await writer.drain()
            except (ConnectionError, OSError):
                dead.append(cid)
        for cid in dead:
            self.clients.pop(cid, None)
            self.client_names.pop(cid, None)
            self.client_ips.pop(cid, None)

    async def start(self, certfile: str = None, keyfile: str = None):
        self._ssl_ctx = None
        if certfile and keyfile:
            self._ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self._ssl_ctx.load_cert_chain(certfile, keyfile)
            log.info('TLS enabled (auto-detect: accepts both TLS and plain connections)')

        # Always listen without SSL — we do per-connection detection instead
        server = await asyncio.start_server(
            self._accept_client, self.host, self.port,
        )

        addrs = ', '.join(str(s.getsockname()) for s in server.sockets)
        log.info('Server listening on %s', addrs)

        async with server:
            await server.serve_forever()

    async def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Peek first byte to decide TLS vs plain, then hand off to handle_client."""
        addr = writer.get_extra_info('peername')
        try:
            first = await asyncio.wait_for(reader.read(1), timeout=10)
            if not first:
                writer.close()
                return

            if first[0] == 0x16 and self._ssl_ctx:
                # TLS ClientHello detected — do handshake via MemoryBIO
                log.info('TLS handshake from %s', addr)
                await self._handle_tls_client(first, reader, writer)
            elif first[0] == 0x16 and not self._ssl_ctx:
                log.warning('TLS connection from %s but no certs configured, rejecting', addr)
                writer.close()
            else:
                # Plain protocol data — create a prefixed reader
                prefixed = _PrefixedReader(first, reader)
                await self.handle_client(prefixed, writer)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            try:
                writer.close()
            except OSError:
                pass

    async def _handle_tls_client(self, first_byte: bytes, raw_reader: asyncio.StreamReader,
                                  raw_writer: asyncio.StreamWriter):
        """Perform TLS handshake over existing connection using ssl.MemoryBIO."""
        incoming = ssl.MemoryBIO()
        outgoing = ssl.MemoryBIO()
        sslobj = self._ssl_ctx.wrap_bio(incoming, outgoing, server_side=True)

        # Feed the first byte we already read
        incoming.write(first_byte)

        # Handshake loop
        while True:
            try:
                sslobj.do_handshake()
                break
            except ssl.SSLWantReadError:
                # Flush outgoing TLS data to client
                out_data = outgoing.read()
                if out_data:
                    raw_writer.write(out_data)
                    await raw_writer.drain()
                # Read more TLS data from client
                data = await asyncio.wait_for(raw_reader.read(16384), timeout=10)
                if not data:
                    raw_writer.close()
                    return
                incoming.write(data)

        # Flush any remaining handshake data
        out_data = outgoing.read()
        if out_data:
            raw_writer.write(out_data)
            await raw_writer.drain()

        addr = raw_writer.get_extra_info('peername')
        log.info('TLS handshake complete for %s', addr)

        # Wrap in a TLS stream adapter
        tls_reader = _TLSReader(sslobj, incoming, raw_reader)
        tls_writer = _TLSWriter(sslobj, outgoing, raw_writer)
        await self.handle_client(tls_reader, tls_writer)


def main():
    parser = argparse.ArgumentParser(description='LAN Game Tunnel - Relay Server')
    parser.add_argument('--host', default='0.0.0.0', help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'Port (default: {DEFAULT_PORT})')
    parser.add_argument('--cert', metavar='FILE', help='TLS certificate file')
    parser.add_argument('--key', metavar='FILE', help='TLS private key file')
    args = parser.parse_args()

    if bool(args.cert) != bool(args.key):
        parser.error('--cert and --key must be used together')

    server = TunnelServer(args.host, args.port)
    try:
        asyncio.run(server.start(args.cert, args.key))
    except KeyboardInterrupt:
        log.info('Server stopped')


if __name__ == '__main__':
    main()
