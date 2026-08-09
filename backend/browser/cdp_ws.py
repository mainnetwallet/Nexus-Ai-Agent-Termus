"""
Minimal, dependency-free async WebSocket client (RFC 6455) -- text frames
only, client role only.

Why this exists instead of `pip install websockets`: that package ships an
optional C extension, and even its pure-Python path is still a third-party
dependency that has to actually install. On Termux, PyPI wheels are built
for glibc ("manylinux") but Android uses Bionic libc, so any wheel with a
compiled component silently fails to install and falls back to a
from-source build -- which then needs a C compiler (`pkg install clang`)
that most Termux setups don't have. `backend/browser/android_backend.py`
needs a WebSocket to talk to Chromium's DevTools Protocol, so rather than
add a fragile dependency for that one need, this implements just enough of
RFC 6455 (client handshake, masked text frames out, unmasked frames in,
basic fragmentation reassembly, close handshake) on top of `asyncio`
streams, which are already available everywhere Python runs.

This is intentionally narrow: no compression (permessage-deflate), no
binary frames (CDP only sends/receives JSON text), no server role. If a
richer WebSocket implementation is ever needed elsewhere, prefer a real
dependency at that point -- this module is scoped to CDP's needs only.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import struct
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("nexus.browser.cdp_ws")

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# WebSocket opcodes we care about (client<->server, text-only protocol).
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WebSocketError(RuntimeError):
    pass


class SimpleWebSocket:
    """One client-side WebSocket connection, opened via `connect()`."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    @classmethod
    async def connect(cls, url: str, timeout: float = 10.0) -> "SimpleWebSocket":
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"Unsupported WebSocket scheme: {parsed.scheme!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=(parsed.scheme == "wss")), timeout=timeout
        )

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()

        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if b"101" not in status_line:
            raise WebSocketError(f"WebSocket handshake failed: {status_line!r}")

        # Drain headers until the blank line; we don't strictly need to
        # verify Sec-WebSocket-Accept for a loopback DevTools connection,
        # but we do need to consume them so they don't pollute the frame
        # stream that follows.
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if line in (b"\r\n", b"\n", b""):
                break

        return cls(reader, writer)

    async def send(self, data: str) -> None:
        if self._closed:
            raise WebSocketError("send() on closed WebSocket")
        payload = data.encode("utf-8")
        self._writer.write(self._encode_frame(_OP_TEXT, payload))
        await self._writer.drain()

    async def recv(self, timeout: Optional[float] = None) -> str:
        """Returns the next complete text message, reassembling fragments
        and transparently answering pings, until one arrives (or the
        connection closes)."""
        while True:
            opcode, payload = await self._read_message(timeout=timeout)
            if opcode == _OP_TEXT:
                return payload.decode("utf-8", errors="replace")
            if opcode == _OP_CLOSE:
                self._closed = True
                raise WebSocketError("WebSocket closed by remote")
            # Pings/pongs/continuations without a preceding text frame are
            # handled inside _read_message; loop for the next real message.

    async def _read_message(self, timeout: Optional[float]) -> tuple[int, bytes]:
        parts: list[bytes] = []
        message_opcode: Optional[int] = None
        while True:
            opcode, fin, payload = await asyncio.wait_for(self._read_frame(), timeout=timeout)
            if opcode == _OP_PING:
                self._writer.write(self._encode_frame(_OP_PONG, payload))
                await self._writer.drain()
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                return _OP_CLOSE, payload
            if opcode != _OP_CONTINUATION:
                message_opcode = opcode
            parts.append(payload)
            if fin:
                return (message_opcode if message_opcode is not None else _OP_TEXT), b"".join(parts)

    async def _read_frame(self) -> tuple[int, bool, bytes]:
        header = await self._reader.readexactly(2)
        b0, b1 = header[0], header[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)  # server frames are never masked, but tolerate it
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
        mask_key = await self._reader.readexactly(4) if masked else None
        payload = await self._reader.readexactly(length) if length else b""
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, fin, payload

    @staticmethod
    def _encode_frame(opcode: int, payload: bytes) -> bytes:
        # Client->server frames MUST be masked per RFC 6455 4-byte mask.
        mask_key = os.urandom(4)
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)  # FIN=1, single-frame messages only
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask_key
        return bytes(header) + masked_payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.write(self._encode_frame(_OP_CLOSE, b""))
            await self._writer.drain()
        except Exception:  # noqa: BLE001 - best-effort close frame
            pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    @property
    def closed(self) -> bool:
        return self._closed
