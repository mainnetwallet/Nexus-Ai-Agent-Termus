"""
Tests for the CDP-driven Android/Termux browser backend:
  - backend/browser/cdp_ws.py       (dependency-free WebSocket client)
  - backend/browser/cdp_client.py   (CDP command/response + event dispatch)
  - backend/browser/android_backend.py (AndroidBrowserBackend availability)
  - backend/browser/factory.py      (platform-based backend selection)

These never launch a real chromium process -- that isn't available in CI
or in this sandbox. Instead a tiny in-process asyncio server plays the
role of Chromium's DevTools WebSocket endpoint, which is enough to verify
the handshake, frame masking/unmasking, and request/response correlation
are all correct. Backend *selection* (factory.py, availability flags) is
tested by monkeypatching `capabilities` / `find_chromium_binary`, the same
pattern test_platform_compat.py already uses for other optional deps.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct

import pytest

from backend.browser.cdp_client import CDPTarget, find_chromium_binary
from backend.browser.cdp_ws import SimpleWebSocket
from backend.platform_info import PlatformCapabilities

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _fake_capabilities(is_android: bool) -> PlatformCapabilities:
    """PlatformCapabilities is a frozen dataclass, so tests substitute a
    whole replacement instance rather than mutating a field (matches the
    pattern already used in test_platform_compat.py)."""
    return PlatformCapabilities(
        system="Android" if is_android else "Linux",
        is_windows=False,
        is_linux=not is_android,
        is_macos=False,
        is_android=is_android,
        psutil_available=False,
        chromadb_available=False,
        playwright_available=False,
    )


def _server_frame(payload: bytes) -> bytes:
    """Encodes a single unmasked server->client text frame (payload < 126 bytes, fine for these tests)."""
    return bytes([0x81, len(payload)]) + payload


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    key = None
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b""):
            break
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
    accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    await writer.drain()


async def _read_client_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(2)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    mask_key = await reader.readexactly(4)
    payload = await reader.readexactly(length)
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


@pytest.mark.asyncio
async def test_websocket_echo_round_trip():
    """Client handshake + masked send + unmasked recv all decode correctly."""

    async def echo_server(reader, writer):
        await _handshake(reader, writer)
        payload = await _read_client_frame(reader)
        writer.write(_server_frame(b"echo:" + payload))
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()

    server = await asyncio.start_server(echo_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        ws = await SimpleWebSocket.connect(f"ws://127.0.0.1:{port}/x")
        await ws.send("hello")
        reply = await ws.recv(timeout=5)
        assert reply == "echo:hello"
        await ws.close()


@pytest.mark.asyncio
async def test_cdp_target_evaluate_and_event_dispatch():
    """CDPTarget correlates a Runtime.evaluate response by id and separately
    updates .url from an unsolicited Page.frameNavigated event."""

    async def cdp_server(reader, writer):
        await _handshake(reader, writer)
        raw = await _read_client_frame(reader)
        req = json.loads(raw)
        assert req["method"] == "Runtime.evaluate"
        writer.write(_server_frame(json.dumps({"id": req["id"], "result": {"result": {"value": 42}}}).encode()))
        writer.write(
            _server_frame(
                json.dumps(
                    {"method": "Page.frameNavigated", "params": {"frame": {"parentId": None, "url": "https://example.com/"}}}
                ).encode()
            )
        )
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(cdp_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        ws = await SimpleWebSocket.connect(f"ws://127.0.0.1:{port}/devtools/page/abc")
        target = CDPTarget("abc", ws)
        target.start_reader()
        result = await target.evaluate("6 * 7")
        assert result == 42
        await asyncio.sleep(0.3)
        assert target.url == "https://example.com/"
        await target.close()


def test_find_chromium_binary_prefers_explicit_path(tmp_path):
    fake_binary = tmp_path / "chromium"
    fake_binary.write_text("#!/bin/sh\n")
    fake_binary.chmod(0o755)
    assert find_chromium_binary(str(fake_binary)) == str(fake_binary)


def test_find_chromium_binary_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_chromium_binary(None) is None


def test_android_backend_unavailable_without_chromium(monkeypatch):
    import backend.browser.android_backend as ab

    monkeypatch.setattr(ab, "CDP_CHROMIUM_PATH", None)
    monkeypatch.setattr(ab, "CDP_BROWSER_AVAILABLE", False)
    monkeypatch.setattr(ab, "capabilities", _fake_capabilities(is_android=True))
    backend = ab.AndroidBrowserBackend()
    assert backend.available is False
    assert "chromium" in (backend.unavailable_reason or "").lower()


def test_android_backend_available_when_chromium_found(monkeypatch):
    import backend.browser.android_backend as ab

    monkeypatch.setattr(ab, "CDP_CHROMIUM_PATH", "/usr/bin/chromium")
    monkeypatch.setattr(ab, "CDP_BROWSER_AVAILABLE", True)
    backend = ab.AndroidBrowserBackend()
    assert backend.available is True
    assert backend.unavailable_reason is None


def test_factory_selects_playwright_engine_on_desktop(monkeypatch):
    import backend.browser.factory as factory_mod
    from backend.browser.engine import BrowserEngine

    monkeypatch.setattr(factory_mod, "capabilities", _fake_capabilities(is_android=False))
    assert isinstance(factory_mod.make_browser_backend(), BrowserEngine)


def test_factory_selects_android_backend_on_android(monkeypatch):
    import backend.browser.factory as factory_mod
    from backend.browser.android_backend import AndroidBrowserBackend

    monkeypatch.setattr(factory_mod, "capabilities", _fake_capabilities(is_android=True))
    assert isinstance(factory_mod.make_browser_backend(), AndroidBrowserBackend)
