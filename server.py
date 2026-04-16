"""LAN Game Tunnel - Relay Server.

Accepts connections from multiple clients and broadcasts
Ethernet frames between them, creating a virtual LAN.
"""

import asyncio
import ssl
import logging
import argparse

from protocol import (
    HEADER_SIZE, DEFAULT_PORT, MAX_FRAME_SIZE,
    MSG_DATA, MSG_HELLO, MSG_KEEPALIVE,
    pack_message, unpack_header,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('tunnel-server')


class TunnelServer:
    def __init__(self, host: str = '0.0.0.0', port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.clients: dict[int, asyncio.StreamWriter] = {}
        self.client_names: dict[int, str] = {}

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
                    name = payload.decode('utf-8', errors='replace')
                    self.client_names[cid] = name
                    log.info('Client %s identified as "%s"', addr, name)
                    self._notify_peers()
                elif msg_type == MSG_KEEPALIVE:
                    writer.write(pack_message(MSG_KEEPALIVE))
                    await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.clients.pop(cid, None)
            self.client_names.pop(cid, None)
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            log.info('Client disconnected: %s', addr)
            self._notify_peers()

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

    def _notify_peers(self):
        """Log current peer count."""
        count = len(self.clients)
        log.info('Connected peers: %d', count)

    async def start(self, certfile: str = None, keyfile: str = None):
        ssl_ctx = None
        if certfile and keyfile:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_ctx.load_cert_chain(certfile, keyfile)
            log.info('TLS enabled')

        server = await asyncio.start_server(
            self.handle_client, self.host, self.port, ssl=ssl_ctx,
        )

        addrs = ', '.join(str(s.getsockname()) for s in server.sockets)
        log.info('Server listening on %s', addrs)

        async with server:
            await server.serve_forever()


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
