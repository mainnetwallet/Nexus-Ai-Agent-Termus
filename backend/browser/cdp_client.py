"""
Low-level Chrome DevTools Protocol (CDP) client.

Talks directly to a `chromium` binary over its `--remote-debugging-port`
HTTP + WebSocket interface -- no Playwright involved. This is what makes
browser automation possible on Android/Termux: Playwright can't install
there (see backend/platform_info.py), but a plain `chromium` package is
installable via `pkg install chromium`, and CDP is just JSON-RPC over a
loopback WebSocket, which `backend/browser/cdp_ws.py` implements with no
third-party dependencies.

Two layers:
  - `CDPBrowser`  -- owns the chromium subprocess + the browser-level HTTP
                     endpoint (list/open/close tabs).
  - `CDPTarget`   -- one tab: owns its own WebSocket, does command/response
                     correlation by request id, and fans out CDP events
                     (Page.loadEventFired, etc.) to registered waiters.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from backend.browser.cdp_ws import SimpleWebSocket, WebSocketError

logger = logging.getLogger("nexus.browser.cdp")

# Names people commonly land on after `pkg install chromium` / on generic
# Linux, checked in order. `settings.browser_cdp_executable_path` always
# wins over this list when set.
_CANDIDATE_BINARIES = (
    "chromium",
    "chromium-browser",
    "chromium.official",
    "chrome",
    "google-chrome",
    "google-chrome-stable",
)


class CDPError(RuntimeError):
    pass


def find_chromium_binary(explicit_path: str | None = None) -> Optional[str]:
    """Resolves a usable chromium/chrome executable path, or None if no
    candidate is on PATH. `explicit_path` (from settings) always wins."""
    if explicit_path:
        return explicit_path if shutil.which(explicit_path) or Path(explicit_path).exists() else None
    for name in _CANDIDATE_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_tcp_port() -> int:
    """Picks an unused localhost port so multiple CDPBrowser instances (or
    a leftover process from a previous run) never fight over 9222."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDPTarget:
    """One browser tab/target: a JSON-RPC session over its own WebSocket."""

    def __init__(self, target_id: str, ws: SimpleWebSocket) -> None:
        self.target_id = target_id
        self._ws = ws
        self._id_counter = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        # One-shot waiters (wait_for_event): consumed on the first matching
        # event, then discarded.
        self._event_waiters: dict[str, list[asyncio.Future]] = {}
        # Persistent listeners (on_event/off_event): kept around and
        # invoked on *every* matching event -- used by screencast streaming
        # (Page.screencastFrame fires repeatedly for as long as the stream
        # is active), unlike wait_for_event's single-shot semantics.
        self._event_listeners: dict[str, list[Callable[[dict], Any]]] = {}
        self._reader_task: asyncio.Task | None = None
        self.url: str = ""

    def start_reader(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while not self._ws.closed:
                raw = await self._ws.recv()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CDPError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result", {}))
                elif "method" in msg:
                    self._handle_event(msg["method"], msg.get("params", {}))
        except (WebSocketError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001 - keep the tab's session alive-ish; callers time out instead
            logger.debug("CDPTarget read loop ended unexpectedly for %s", self.target_id, exc_info=True)

    def _handle_event(self, method: str, params: dict) -> None:
        if method == "Page.frameNavigated" and params.get("frame", {}).get("parentId") is None:
            self.url = params["frame"].get("url", self.url)
        waiters = self._event_waiters.pop(method, None)
        if waiters:
            for fut in waiters:
                if not fut.done():
                    fut.set_result(params)
        for callback in list(self._event_listeners.get(method, ())):
            try:
                result = callback(params)
            except Exception:  # noqa: BLE001 - one bad listener must not kill the read loop
                logger.exception("CDP event listener for %s raised", method)
                continue
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    async def send(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        req_id = next(self._id_counter)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise CDPError(f"CDP call {method} timed out after {timeout}s") from exc

    async def wait_for_event(self, method: str, timeout: float = 15.0) -> dict:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._event_waiters.setdefault(method, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CDPError(f"Timed out waiting for CDP event {method}") from exc

    def on_event(self, method: str, callback: Callable[[dict], Any]) -> None:
        """Registers `callback` to run on *every* future `method` event
        (sync or async -- an async callback's coroutine is scheduled via
        `asyncio.create_task`), unlike `wait_for_event` which resolves once
        and forgets. Used for repeating streams like
        `Page.screencastFrame`. Call `off_event` with the same callback to
        stop receiving events."""
        self._event_listeners.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[[dict], Any]) -> None:
        listeners = self._event_listeners.get(method)
        if not listeners:
            return
        try:
            listeners.remove(callback)
        except ValueError:
            pass
        if not listeners:
            self._event_listeners.pop(method, None)

    async def evaluate(self, expression: str, await_promise: bool = True, timeout: float = 30.0) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
            timeout=timeout,
        )
        exc_details = result.get("exceptionDetails")
        if exc_details:
            raise CDPError(exc_details.get("exception", {}).get("description", str(exc_details)))
        return result.get("result", {}).get("value")

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        await self._ws.close()


class CDPBrowser:
    """Owns the chromium subprocess and the browser-level HTTP control
    surface (target list/create/close), reachable at
    http://127.0.0.1:{port}/json/*."""

    def __init__(
        self,
        executable_path: str,
        user_data_dir: str,
        headless: bool = True,
        port: int | None = None,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        self._executable_path = executable_path
        self._user_data_dir = user_data_dir
        self._headless = headless
        self._port = port or _free_tcp_port()
        self._extra_args = extra_args or []
        self._process: subprocess.Popen | None = None
        self._http = httpx.AsyncClient(timeout=10.0)
        self._targets: dict[str, CDPTarget] = {}

    @property
    def port(self) -> int:
        return self._port

    @property
    def http_base(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def start(self, launch_timeout: float = 20.0) -> None:
        Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
        args = [
            self._executable_path,
            f"--remote-debugging-port={self._port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self._user_data_dir}",
            "--no-sandbox",  # required: Termux has no setuid sandbox helper
            "--disable-gpu",
            "--disable-dev-shm-usage",  # Termux tmpfs is often tiny
            "--disable-extensions",
            "--disable-background-networking",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,900",
        ]
        if self._headless:
            args.append("--headless=new")
        args.extend(self._extra_args)

        logger.info("Launching chromium via CDP on port %d", self._port)
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

        deadline = time.monotonic() + launch_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise CDPError(
                    f"chromium exited early (code={self._process.returncode}) -- "
                    "try running the same command manually in Termux to see its stderr"
                )
            try:
                resp = await self._http.get(f"{self.http_base}/json/version")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError as exc:
                last_error = exc
            await asyncio.sleep(0.2)
        self.kill()
        raise CDPError(f"chromium did not open its DevTools port in time ({last_error})")

    async def new_target(self, url: str = "about:blank") -> CDPTarget:
        resp = await self._http.put(f"{self.http_base}/json/new", params={"url": url})
        resp.raise_for_status()
        info = resp.json()
        target_id = info["id"]
        ws_url = info["webSocketDebuggerUrl"]
        ws = await SimpleWebSocket.connect(ws_url)
        target = CDPTarget(target_id, ws)
        target.url = info.get("url", url)
        target.start_reader()
        for method in ("Page.enable", "Runtime.enable", "DOM.enable", "Network.enable"):
            await target.send(method)
        self._targets[target_id] = target
        return target

    async def close_target(self, target_id: str) -> None:
        target = self._targets.pop(target_id, None)
        if target:
            try:
                await target.close()
            except Exception as exc:
                logger.debug("close_target: websocket close failed for %s (%s)", target_id, exc)
        try:
            await self._http.get(f"{self.http_base}/json/close/{target_id}")
        except httpx.HTTPError:
            pass

    def kill(self) -> None:
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self._process.kill()
                except Exception:  # noqa: BLE001
                    pass

    async def stop(self) -> None:
        """Closing individual targets is best-effort -- one already-dead
        websocket or a page the site itself closed must never stop this from
        reaching self.kill() at the end, or the chromium subprocess (and its
        visible window) is left running indefinitely."""
        for target_id in list(self._targets.keys()):
            try:
                await self.close_target(target_id)
            except Exception as exc:
                logger.debug("stop: close_target failed for %s (%s)", target_id, exc)
        try:
            await self._http.aclose()
        except Exception as exc:
            logger.debug("stop: http client aclose failed (%s)", exc)
        self.kill()
