"""
AndroidBrowserBackend -- CDP-driven browser automation for Android/Termux.

Playwright cannot install natively under Termux (no matching Chromium/
driver build for Bionic libc), so this backend does not use Playwright at
all. Instead it drives a plain `chromium` binary (installable via
`pkg install chromium`) directly over the Chrome DevTools Protocol (CDP),
using `backend/browser/cdp_client.py` + `backend/browser/cdp_ws.py`, which
have zero third-party dependencies -- important on Termux where wheels
with compiled components frequently fail to install.

This mirrors `BrowserEngine`'s public surface (backend/browser/engine.py)
closely enough that `backend/planner/task_queue.py` and the MCP connectors
(backend/mcp/connectors/*) can use either one interchangeably; see
`backend/browser/factory.py` for the platform-based selection. Some of
Playwright's guarantees don't carry over 1:1 -- most notably, this can only
interact with elements in the main document; same-origin iframes are
reachable via CDP's per-frame execution contexts in principle, but that's
not implemented here, so cross-origin embedded challenges (e.g. some
CAPTCHA iframes) won't be solvable the way engine.py's iframe-traversal
code attempts to.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from backend.browser.backend_base import BrowserBackend
from backend.browser.cdp_client import CDPBrowser, CDPError, CDPTarget, find_chromium_binary
from backend.browser.engine import BrowserEngineError, PageSnapshot
from backend.config.settings import settings, SCREENSHOT_DIR
from backend.platform_info import capabilities

logger = logging.getLogger("nexus.browser.android")

#: True only when a chromium/chrome binary is actually resolvable on PATH
#: (or at settings.browser_executable_path). Android/Termux is the only
#: platform this backend is offered on -- see factory.py.
CDP_CHROMIUM_PATH = find_chromium_binary(settings.browser_executable_path)
CDP_BROWSER_AVAILABLE = capabilities.is_android and CDP_CHROMIUM_PATH is not None


class _Keyboard:
    """Minimal `page.keyboard`-shaped shim so call sites written against
    Playwright's `engine.page.keyboard.press("Enter")` keep working
    unchanged (see backend/mcp/connectors/discord_connector.py)."""

    _KEY_CODES = {
        "Enter": (13, "Enter", "Enter"),
        "Tab": (9, "Tab", "Tab"),
        "Escape": (27, "Escape", "Escape"),
        "Backspace": (8, "Backspace", "Backspace"),
        "ArrowDown": (40, "ArrowDown", "ArrowDown"),
        "ArrowUp": (38, "ArrowUp", "ArrowUp"),
    }

    def __init__(self, target: CDPTarget) -> None:
        self._target = target

    async def press(self, key: str) -> None:
        vk, code, key_name = self._KEY_CODES.get(key, (0, key, key))
        common = {"key": key_name, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        await self._target.send("Input.dispatchKeyEvent", {"type": "keyDown", **common})
        await self._target.send("Input.dispatchKeyEvent", {"type": "keyUp", **common})


class _PageShim:
    """Minimal `page`-shaped shim exposing just what call sites outside
    this module read directly (`engine.page.url`, `engine.page.keyboard`).
    Everything else goes through AndroidBrowserBackend's own methods."""

    def __init__(self, target: CDPTarget) -> None:
        self._target = target
        self.keyboard = _Keyboard(target)

    @property
    def url(self) -> str:
        return self._target.url


# JS shared conceptually with engine.py's extraction logic (kept in sync by
# hand since this backend can't reuse Playwright-side helpers).
_EXTRACT_ELEMENTS_JS = """
(function(limit) {
    const out = [];
    const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const selectors = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [onclick]';
    const nodes = Array.from(document.querySelectorAll(selectors));
    for (const el of nodes) {
        if (!isVisible(el)) continue;
        const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 120);
        if (!text && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') continue;
        let selector = '';
        if (el.id) { selector = '#' + el.id; }
        else if (el.getAttribute('name')) { selector = '[name="' + el.getAttribute('name') + '"]'; }
        else if (el.placeholder) { selector = '[placeholder="' + el.placeholder + '"]'; }
        else if (el.getAttribute('aria-label')) { selector = '[aria-label="' + el.getAttribute('aria-label') + '"]'; }
        else if (text) { selector = el.tagName.toLowerCase() + ':has-text("' + text.slice(0, 30).replace(/"/g, '') + '")'; }
        else { selector = el.tagName.toLowerCase(); }
        out.push({
            tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', text: text,
            selector: selector, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
        });
        if (out.length >= limit) break;
    }
    return out;
})(%d)
"""

_SMART_CLICK_JS = r"""
(function(target) {
    const norm = (s) => (s || '').trim().toLowerCase();
    const wanted = norm(target);
    const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    let el = null;
    try { el = document.querySelector(target); } catch (e) { el = null; }
    if (!el || !isVisible(el)) {
        const candidates = Array.from(document.querySelectorAll(
            'a, button, [role="button"], [role="link"], input[type="submit"], input[type="button"]'
        ));
        el = candidates.find((c) => isVisible(c) && (
            norm(c.innerText).includes(wanted) ||
            norm(c.getAttribute('aria-label')).includes(wanted) ||
            norm(c.value).includes(wanted)
        )) || null;
    }
    if (!el) return false;
    el.scrollIntoView({block: 'center'});
    el.click();
    return true;
})(%s)
"""

_SMART_TYPE_JS = r"""
(function(target, value) {
    const norm = (s) => (s || '').trim().toLowerCase();
    const wanted = norm(target);
    const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    let el = null;
    try { el = document.querySelector(target); } catch (e) { el = null; }
    if (!el || !isVisible(el)) {
        const candidates = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'));
        el = candidates.find((c) => isVisible(c) && (
            norm(c.placeholder).includes(wanted) ||
            norm(c.getAttribute('aria-label')).includes(wanted) ||
            norm(c.name).includes(wanted) ||
            norm(c.id).includes(wanted)
        )) || candidates.find(isVisible) || null;
    }
    if (!el) return false;
    el.scrollIntoView({block: 'center'});
    el.focus();
    if (el.isContentEditable) {
        el.innerText = value;
    } else {
        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
})(%s, %s)
"""


class AndroidBrowserBackend(BrowserBackend):
    name = "android_cdp"

    @property
    def available(self) -> bool:
        return CDP_BROWSER_AVAILABLE

    @property
    def unavailable_reason(self) -> str | None:
        if CDP_BROWSER_AVAILABLE:
            return None
        if not capabilities.is_android:
            return "AndroidBrowserBackend is only used on Android/Termux."
        return (
            "No chromium/chrome binary found on PATH. Install one with "
            "`pkg install chromium` (Termux), or set browser_executable_path."
        )

    def __init__(self, headless: bool | None = None, user_data_dir: str | None = None) -> None:
        self._headless = settings.browser_headless if headless is None else headless
        self._user_data_dir = user_data_dir or settings.browser_user_data_dir or str(
            Path(SCREENSHOT_DIR).parent / "data" / "chromium-profile"
        )
        self._browser: Optional[CDPBrowser] = None
        self._targets: dict[str, CDPTarget] = {}
        self._active_id: Optional[str] = None

    @property
    def user_data_dir(self) -> str | None:
        return self._user_data_dir

    @property
    def page(self) -> _PageShim:
        if not self._active_id or self._active_id not in self._targets:
            raise BrowserEngineError("No active page")
        return _PageShim(self._targets[self._active_id])

    def _target(self) -> CDPTarget:
        if not self._active_id or self._active_id not in self._targets:
            raise BrowserEngineError("No active page")
        return self._targets[self._active_id]

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if not CDP_BROWSER_AVAILABLE:
            raise BrowserEngineError(
                self.unavailable_reason or "Chromium is unavailable; browser automation is disabled."
            )
        self._browser = CDPBrowser(
            executable_path=CDP_CHROMIUM_PATH,  # type: ignore[arg-type]
            user_data_dir=self._user_data_dir,
            headless=self._headless,
        )
        await self._browser.start()
        await self.new_tab()
        logger.info("Android CDP browser started (port=%d headless=%s)", self._browser.port, self._headless)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.stop()
        self._targets.clear()
        self._active_id = None
        logger.info("Android CDP browser stopped")

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #
    async def new_tab(self, url: Optional[str] = None, wait_until: str = "domcontentloaded") -> str:
        if self._browser is None:
            raise BrowserEngineError("Browser backend not started")
        target = await self._browser.new_target(url or "about:blank")
        page_id = str(uuid.uuid4())[:8]
        self._targets[page_id] = target
        previous_active = self._active_id
        self._active_id = page_id
        if url:
            try:
                await self.navigate(url, wait_until=wait_until)
            except Exception:
                logger.debug("new_tab: navigation to %r failed", url)
        else:
            self._active_id = previous_active or page_id
        return page_id

    def switch_tab(self, page_id: str) -> None:
        if page_id not in self._targets:
            raise BrowserEngineError(f"Unknown page id {page_id}")
        self._active_id = page_id

    def list_tabs(self) -> list[dict[str, str]]:
        return [{"id": pid, "url": t.url, "title": ""} for pid, t in self._targets.items()]

    async def close_tab(self, page_id: str) -> None:
        target = self._targets.pop(page_id, None)
        if target is None or self._browser is None:
            return
        await self._browser.close_target(target.target_id)
        if self._active_id == page_id:
            self._active_id = next(iter(self._targets), None)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        target = self._target()
        await target.send("Page.navigate", {"url": url}, timeout=settings.browser_default_timeout_ms / 1000)
        await self._settle()

    async def go_back(self) -> None:
        await self._target().evaluate("history.back()")
        await self._settle()

    async def _settle(self, ms: int = 400) -> None:
        target = self._target()
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                state = await target.evaluate("document.readyState")
                if state == "complete":
                    break
                await asyncio.sleep(0.15)
        except CDPError:
            pass
        await asyncio.sleep(ms / 1000)

    async def smart_wait(self, condition: str = "networkidle", timeout_ms: int = 10_000) -> None:
        if condition in ("load", "domcontentloaded", "networkidle"):
            target = self._target()
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                try:
                    if await target.evaluate("document.readyState") == "complete":
                        break
                except CDPError:
                    pass
                await asyncio.sleep(0.15)
            if condition == "networkidle":
                await asyncio.sleep(0.5)  # best-effort settle; no Network-domain idle tracking
        else:
            await asyncio.sleep(timeout_ms / 1000)

    # ------------------------------------------------------------------ #
    # Smart primitives
    # ------------------------------------------------------------------ #
    async def smart_click(self, selector_or_text: str, exact: bool = False, timeout_ms: int | None = None) -> bool:
        text_clean = (selector_or_text or "").strip()
        if not text_clean:
            return False
        raw_text = text_clean
        if ":has-text(" in text_clean:
            import re
            m = re.search(r':has-text\(["\']?(.*?)["\']?\)', text_clean)
            if m:
                raw_text = m.group(1).rstrip("\"')")
        elif text_clean.startswith(("button:", "input:", "a:")):
            raw_text = text_clean.split(":", 1)[1].strip()

        try:
            clicked = await self._target().evaluate(_SMART_CLICK_JS % json.dumps(raw_text))
        except CDPError:
            clicked = False
        if clicked:
            await self._settle()
            return True
        logger.warning("smart_click failed for %r", selector_or_text)
        return False

    async def smart_type(self, selector_or_label: str, text: str, clear_first: bool = True) -> bool:
        text_clean = (selector_or_label or "").strip()
        raw_text = text_clean
        if ":has-text(" in text_clean:
            import re
            m = re.search(r':has-text\(["\']?(.*?)["\']?\)', text_clean)
            if m:
                raw_text = m.group(1).rstrip("\"')")
        elif text_clean.startswith(("button:", "input:", "a:", "textarea:")):
            raw_text = text_clean.split(":", 1)[1].strip()

        try:
            typed = await self._target().evaluate(_SMART_TYPE_JS % (json.dumps(raw_text), json.dumps(text)))
        except CDPError:
            typed = False
        if typed:
            enter = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
            await self._target().send("Input.dispatchKeyEvent", {"type": "keyDown", **enter})
            await self._target().send("Input.dispatchKeyEvent", {"type": "keyUp", **enter})
            await self._settle(ms=300)
            return True
        logger.warning("smart_type failed for %r", selector_or_label)
        return False

    async def smart_scroll(self, direction: str = "down", amount_px: int = 800) -> None:
        delta = amount_px if direction == "down" else -amount_px
        await self._target().evaluate(f"window.scrollBy(0, {delta})")
        await asyncio.sleep(0.2)

    async def upload_file(self, selector: str, file_path: str) -> bool:
        try:
            target = self._target()
            doc = await target.send("DOM.getDocument")
            root_id = doc["root"]["nodeId"]
            found = await target.send("DOM.querySelector", {"nodeId": root_id, "selector": selector})
            node_id = found.get("nodeId")
            if not node_id:
                return False
            await target.send("DOM.setFileInputFiles", {"files": [file_path], "nodeId": node_id})
            return True
        except Exception:
            logger.warning("upload_file failed for selector=%s", selector)
            return False

    # ------------------------------------------------------------------ #
    # Perception
    # ------------------------------------------------------------------ #
    async def extract_interactive_elements(self, limit: int = 150) -> list[dict[str, Any]]:
        try:
            result = await self._target().evaluate(_EXTRACT_ELEMENTS_JS % limit)
            return result or []
        except CDPError:
            logger.exception("extract_interactive_elements failed")
            return []

    async def extract_visible_text(self, max_chars: int = 6000) -> str:
        try:
            text = await self._target().evaluate("document.body ? document.body.innerText : ''")
            return (text or "")[:max_chars]
        except CDPError:
            return ""

    async def screenshot(self, name_hint: str = "step") -> str:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{name_hint}_{int(time.time() * 1000)}.png"
        result = await self._target().send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(result["data"])
        path.write_bytes(data)
        return str(path)

    async def eval_js(self, expression: str, default: Any = None) -> Any:
        try:
            return await self._target().evaluate(expression)
        except CDPError:
            logger.debug("eval_js failed for expression=%r", expression[:120])
            return default

    async def get_injected_wallet_state(self) -> dict[str, Any]:
        js = """
        (() => {
            const eth = window.ethereum;
            if (!eth) return { present: false };
            return {
                present: true,
                isConnected: (typeof eth.isConnected === 'function') ? eth.isConnected() : null,
                chainId: eth.chainId || null,
                selectedAddress: eth.selectedAddress || null,
                isMetaMask: !!eth.isMetaMask,
            };
        })()
        """
        return await self.eval_js(js, default={"present": False})

    async def auto_handle_security_verification(self) -> bool:
        """Best-effort, main-frame-only checkbox click for simple challenge
        widgets. Unlike engine.py's Playwright version, this cannot reach
        into cross-origin iframes (Cloudflare Turnstile, hCaptcha, etc. are
        usually rendered in one), so it only helps with same-document
        challenges; anything iframe-based needs a person to solve it via a
        manual session instead."""
        js = """
        (() => {
            const sel = ['input[type="checkbox"]', '#recaptcha-anchor', '.recaptcha-checkbox-border', '#checkbox'];
            for (const s of sel) {
                const el = document.querySelector(s);
                if (el) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) { el.click(); return true; }
                }
            }
            return false;
        })()
        """
        try:
            return bool(await self._target().evaluate(js))
        except CDPError:
            return False

    async def snapshot(self, name_hint: str = "snapshot") -> PageSnapshot:
        await self.auto_handle_security_verification()
        target = self._target()
        title = await self.eval_js("document.title", default="")
        return PageSnapshot(
            url=target.url,
            title=title or "",
            visible_text=await self.extract_visible_text(),
            interactive_elements=await self.extract_interactive_elements(),
            screenshot_path=await self.screenshot(name_hint),
        )

    async def detect_popup_or_dialog(self, timeout_ms: int = 2_000) -> Optional[str]:
        await asyncio.sleep(timeout_ms / 1000)
        if len(self._targets) > 1:
            return list(self._targets.keys())[-1]
        return None
