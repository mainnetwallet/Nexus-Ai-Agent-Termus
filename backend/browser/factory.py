"""
Picks the right browser backend for the current platform in one place.

Desktop (Windows/Linux/macOS): `BrowserEngine` (Playwright).
Android/Termux: `AndroidBrowserBackend` (CDP against a `pkg install
chromium` binary) when one is resolvable, else a backend whose
`.available` is False with a clear `.unavailable_reason` -- callers that
already check `.available` before driving the engine (see
backend/browser/backend_base.py) keep working without changes.
"""
from __future__ import annotations

from typing import Union

from backend.browser.backend_base import BrowserBackend
from backend.browser.engine import BrowserEngine
from backend.platform_info import capabilities

# Imported at module scope (not lazily) so the Union below is a real type,
# not a forward-reference string -- android_backend.py has no import back
# onto this module, so there's no circularity to worry about.
from backend.browser.android_backend import AndroidBrowserBackend

AnyBrowserBackend = Union[BrowserEngine, AndroidBrowserBackend]


def make_browser_backend(
    headless: bool | None = None,
    user_data_dir: str | None = None,
) -> AnyBrowserBackend:
    """Returns a not-yet-started browser backend instance appropriate for
    this platform. Callers still call `.start()` / `.stop()` themselves --
    this only decides *which* backend to construct."""
    if capabilities.is_android:
        return AndroidBrowserBackend(headless=headless, user_data_dir=user_data_dir)
    return BrowserEngine(headless=headless, user_data_dir=user_data_dir)
