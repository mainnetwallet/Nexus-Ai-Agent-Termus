"""
Tests for the event-driven "live browser view" screencast path:
  - backend/browser/cdp_client.py    (CDPTarget.on_event/off_event -- a
    persistent, repeating-event subscription, unlike wait_for_event's
    one-shot semantics)
  - backend/browser/engine.py        (BrowserEngine.start_screencast /
    stop_screencast via Playwright's raw CDP session)
  - backend/browser/android_backend.py (AndroidBrowserBackend.start_screencast
    / stop_screencast via CDPTarget directly)
  - backend/browser/live_session.py  (LiveSessionManager preferring the
    screencast when available, falling back to page.screenshot() polling
    when it isn't)

Like test_android_browser_backend.py, the CDPTarget-level test drives a
tiny in-process asyncio server that speaks just enough of the CDP
WebSocket wire format to be useful -- no real chromium involved. The
higher-level engine/backend/live_session tests use lightweight fakes
instead, the same way test_page_shim_screenshot_and_title_used_by_live_session
does for the Android page shim, since what's being verified there is the
calling convention (params sent, frames acked, fallback on failure), not
the wire protocol itself.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from backend.browser.android_backend import AndroidBrowserBackend
from backend.browser.cdp_client import CDPError, CDPTarget
from backend.browser.cdp_ws import SimpleWebSocket
from backend.browser.engine import BrowserEngine, BrowserEngineError
from backend.browser.live_session import LiveSessionManager

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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


def _server_frame(payload: bytes) -> bytes:
    """Encodes a single unmasked server->client text frame. Handles both
    the <126-byte and 16-bit-extended-length cases (screencast JSON frames
    are bigger than the plain CDP messages test_android_browser_backend.py
    sends)."""
    length = len(payload)
    if length < 126:
        header = bytes([0x81, length])
    else:
        header = bytes([0x81, 126]) + length.to_bytes(2, "big")
    return header + payload


async def _read_client_frame(reader: asyncio.StreamReader) -> bytes:
    import struct

    header = await reader.readexactly(2)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    mask_key = await reader.readexactly(4)
    payload = await reader.readexactly(length)
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


# --------------------------------------------------------------------- #
# CDPTarget.on_event/off_event -- persistent (repeating) event listeners
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_event_fires_for_every_matching_event_and_acks_each():
    """Unlike wait_for_event (one-shot), on_event must keep firing for
    every Page.screencastFrame the server pushes, and each frame must be
    acked back -- Chrome stops streaming otherwise."""
    acks = []

    async def cdp_server(reader, writer):
        await _handshake(reader, writer)
        for i in range(1, 4):
            writer.write(
                _server_frame(
                    json.dumps(
                        {
                            "method": "Page.screencastFrame",
                            "params": {"data": base64.b64encode(f"frame-{i}".encode()).decode(), "sessionId": i},
                        }
                    ).encode()
                )
            )
            await writer.drain()
            ack_raw = await _read_client_frame(reader)
            ack = json.loads(ack_raw)
            assert ack["method"] == "Page.screencastFrameAck"
            acks.append(ack["params"]["sessionId"])
            writer.write(_server_frame(json.dumps({"id": ack["id"], "result": {}}).encode()))
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(cdp_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        ws = await SimpleWebSocket.connect(f"ws://127.0.0.1:{port}/devtools/page/abc")
        target = CDPTarget("abc", ws)
        target.start_reader()

        received: list[bytes] = []

        async def on_frame(params: dict) -> None:
            received.append(base64.b64decode(params["data"]))
            await target.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

        target.on_event("Page.screencastFrame", on_frame)
        await asyncio.sleep(0.5)
        await target.close()

        assert received == [b"frame-1", b"frame-2", b"frame-3"]
        assert acks == [1, 2, 3]


@pytest.mark.asyncio
async def test_off_event_stops_further_callback_invocations():
    async def cdp_server(reader, writer):
        await _handshake(reader, writer)
        writer.write(
            _server_frame(
                json.dumps(
                    {"method": "Page.screencastFrame", "params": {"data": base64.b64encode(b"a").decode(), "sessionId": 1}}
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

        calls = []

        def on_frame(params: dict) -> None:
            calls.append(params)

        target.on_event("Page.screencastFrame", on_frame)
        target.off_event("Page.screencastFrame", on_frame)
        await asyncio.sleep(0.3)
        await target.close()

        assert calls == []


# --------------------------------------------------------------------- #
# AndroidBrowserBackend.start_screencast / stop_screencast
# --------------------------------------------------------------------- #
class _FakeCDPTarget:
    """Minimal stand-in for CDPTarget exposing just send/on_event/off_event,
    the same lightweight-fake approach test_android_browser_backend.py
    uses for _PageShim."""

    def __init__(self, fail_start: bool = False) -> None:
        self.url = "https://example.com/"
        self.sent: list[tuple[str, dict | None]] = []
        self._listeners: dict[str, list] = {}
        self._fail_start = fail_start

    async def send(self, method, params=None, timeout=30.0):
        self.sent.append((method, params))
        if method == "Page.startScreencast" and self._fail_start:
            raise CDPError("boom")
        return {}

    def on_event(self, method, callback):
        self._listeners.setdefault(method, []).append(callback)

    def off_event(self, method, callback):
        listeners = self._listeners.get(method, [])
        if callback in listeners:
            listeners.remove(callback)

    def emit(self, method, params):
        for cb in list(self._listeners.get(method, [])):
            result = cb(params)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)


@pytest.mark.asyncio
async def test_android_backend_start_screencast_streams_frames_and_acks():
    backend = AndroidBrowserBackend()
    fake_target = _FakeCDPTarget()
    backend._targets["p1"] = fake_target  # type: ignore[assignment]
    backend._active_id = "p1"

    received = []

    async def on_frame(frame_bytes, meta):
        received.append(frame_bytes)

    ok = await backend.start_screencast(on_frame, quality=55, max_width=800, max_height=600)
    assert ok is True

    start_calls = [p for m, p in fake_target.sent if m == "Page.startScreencast"]
    assert start_calls == [{"format": "jpeg", "quality": 55, "maxWidth": 800, "maxHeight": 600, "everyNthFrame": 1}]

    fake_target.emit("Page.screencastFrame", {"data": base64.b64encode(b"jpeg-bytes").decode(), "sessionId": 7})
    await asyncio.sleep(0.05)

    assert received == [b"jpeg-bytes"]
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in fake_target.sent

    await backend.stop_screencast()
    assert ("Page.stopScreencast", None) in fake_target.sent
    assert backend._screencast_target is None
    # Listener detached -- a further emit must not call on_frame again.
    fake_target.emit("Page.screencastFrame", {"data": base64.b64encode(b"ignored").decode(), "sessionId": 8})
    await asyncio.sleep(0.05)
    assert received == [b"jpeg-bytes"]


@pytest.mark.asyncio
async def test_android_backend_start_screencast_returns_false_on_failure():
    backend = AndroidBrowserBackend()
    fake_target = _FakeCDPTarget(fail_start=True)
    backend._targets["p1"] = fake_target  # type: ignore[assignment]
    backend._active_id = "p1"

    async def on_frame(frame_bytes, meta):
        pass

    ok = await backend.start_screencast(on_frame)
    assert ok is False
    assert backend._screencast_target is None
    assert fake_target._listeners.get("Page.screencastFrame", []) == []


@pytest.mark.asyncio
async def test_android_backend_start_screencast_no_active_page_returns_false():
    backend = AndroidBrowserBackend()

    async def on_frame(frame_bytes, meta):
        pass

    assert await backend.start_screencast(on_frame) is False


# --------------------------------------------------------------------- #
# BrowserEngine.start_screencast / stop_screencast (Playwright side)
# --------------------------------------------------------------------- #
class _FakeCDPSession:
    def __init__(self, fail_start: bool = False) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self._listeners: dict[str, list] = {}
        self.detached = False
        self._fail_start = fail_start

    def on(self, method, callback):
        self._listeners.setdefault(method, []).append(callback)

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Page.startScreencast" and self._fail_start:
            raise RuntimeError("boom")
        return {}

    async def detach(self):
        self.detached = True

    def emit(self, method, params):
        for cb in list(self._listeners.get(method, [])):
            cb(params)


class _FakeContext:
    def __init__(self, session: _FakeCDPSession) -> None:
        self._session = session
        self.new_cdp_session_calls = 0

    async def new_cdp_session(self, page):
        self.new_cdp_session_calls += 1
        return self._session


class _FakePage:
    url = "https://example.com/"


@pytest.mark.asyncio
async def test_browser_engine_start_screencast_streams_frames_and_acks():
    engine = BrowserEngine.__new__(BrowserEngine)  # bypass __init__ (needs Playwright/settings wiring)
    session = _FakeCDPSession()
    engine._context = _FakeContext(session)
    engine._pages = {"p1": _FakePage()}
    engine._active_page_id = "p1"
    engine._screencast_session = None

    received = []

    async def on_frame(frame_bytes, meta):
        received.append(frame_bytes)

    ok = await engine.start_screencast(on_frame, quality=70, max_width=1280, max_height=900)
    assert ok is True
    assert engine._context.new_cdp_session_calls == 1

    start_calls = [p for m, p in session.sent if m == "Page.startScreencast"]
    assert start_calls == [{"format": "jpeg", "quality": 70, "maxWidth": 1280, "maxHeight": 900, "everyNthFrame": 1}]

    session.emit("Page.screencastFrame", {"data": base64.b64encode(b"jpeg-bytes").decode(), "sessionId": 3})
    await asyncio.sleep(0.05)

    assert received == [b"jpeg-bytes"]
    assert ("Page.screencastFrameAck", {"sessionId": 3}) in session.sent

    await engine.stop_screencast()
    assert ("Page.stopScreencast", None) in session.sent
    assert session.detached is True
    assert engine._screencast_session is None


@pytest.mark.asyncio
async def test_browser_engine_start_screencast_returns_false_on_failure():
    engine = BrowserEngine.__new__(BrowserEngine)
    session = _FakeCDPSession(fail_start=True)
    engine._context = _FakeContext(session)
    engine._pages = {"p1": _FakePage()}
    engine._active_page_id = "p1"
    engine._screencast_session = None

    async def on_frame(frame_bytes, meta):
        pass

    ok = await engine.start_screencast(on_frame)
    assert ok is False
    assert engine._screencast_session is None
    assert session.detached is True  # cleaned up after the failed start


@pytest.mark.asyncio
async def test_browser_engine_start_screencast_no_active_page_returns_false():
    engine = BrowserEngine.__new__(BrowserEngine)
    engine._context = _FakeContext(_FakeCDPSession())
    engine._pages = {}
    engine._active_page_id = None
    engine._screencast_session = None

    async def on_frame(frame_bytes, meta):
        pass

    with pytest.raises(BrowserEngineError):
        engine.page  # sanity: no active page

    assert await engine.start_screencast(on_frame) is False


# --------------------------------------------------------------------- #
# LiveSessionManager: prefers screencast, falls back to polling
# --------------------------------------------------------------------- #
class _FakeScreencastEngine:
    """Engine stub whose start_screencast succeeds and pushes frames
    on-demand via `.push_frame()`, mirroring BrowserEngine/AndroidBrowserBackend's
    public surface as seen by LiveSessionManager."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self._on_frame = None

        class _Page:
            url = "https://example.com/live"

            async def title(self):
                return "Live Page"

        self.page = _Page()

    async def start_screencast(self, on_frame, *, quality, max_width, max_height, every_nth_frame):
        self.start_calls += 1
        self._on_frame = on_frame
        return True

    async def stop_screencast(self):
        self.stop_calls += 1

    async def push_frame(self, data: bytes):
        result = self._on_frame(data, {"sessionId": 1})
        if asyncio.iscoroutine(result):
            await result


class _FakePollOnlyEngine:
    """Engine stub with no start_screencast at all -- exercises the
    backward-compatible polling fallback."""

    def __init__(self) -> None:
        class _Page:
            url = "https://example.com/poll"

            async def screenshot(self, type="jpeg", quality=60, full_page=False, **kwargs):
                return b"\xff\xd8poll-bytes"

            async def title(self):
                return "Poll Page"

        self.page = _Page()


def _manager(engine_box, interval_ms=20) -> LiveSessionManager:
    return LiveSessionManager(
        engine_provider=lambda: engine_box["engine"],
        task_id_provider=lambda: "task-1" if engine_box["engine"] else None,
        interval_ms=interval_ms,
        jpeg_quality=55,
    )


@pytest.mark.asyncio
async def test_live_session_prefers_screencast_when_supported():
    engine = _FakeScreencastEngine()
    box = {"engine": engine}
    manager = _manager(box)

    manager.start()
    await asyncio.sleep(0.05)
    assert manager._capture_mode == "screencast"
    assert engine.start_calls == 1

    await engine.push_frame(b"frame-bytes")
    await asyncio.sleep(0.02)

    assert manager.latest_screenshot_bytes() == b"frame-bytes"
    status = manager.status()
    assert status["frame_count"] == 1
    assert status["url"] == "https://example.com/live"

    await manager.stop()
    assert engine.stop_calls == 1


@pytest.mark.asyncio
async def test_live_session_falls_back_to_polling_without_screencast_support():
    engine = _FakePollOnlyEngine()
    box = {"engine": engine}
    manager = _manager(box)

    manager.start()
    await asyncio.sleep(0.08)
    assert manager._capture_mode == "poll"
    assert manager.latest_screenshot_bytes() == b"\xff\xd8poll-bytes"

    await manager.stop()


@pytest.mark.asyncio
async def test_live_session_switches_engine_stops_old_screencast():
    engine1 = _FakeScreencastEngine()
    box = {"engine": engine1}
    manager = _manager(box)

    manager.start()
    await asyncio.sleep(0.05)
    assert manager._capture_mode == "screencast"
    assert engine1.start_calls == 1

    engine2 = _FakeScreencastEngine()
    box["engine"] = engine2
    await asyncio.sleep(0.05)

    assert engine1.stop_calls == 1
    assert engine2.start_calls == 1

    await manager.stop()
    assert engine2.stop_calls == 1
