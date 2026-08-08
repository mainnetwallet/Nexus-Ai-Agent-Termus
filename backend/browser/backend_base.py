"""
BrowserBackend -- a thin capability marker mixed into concrete browser
engines so callers (diagnostics, the dashboard, future automation code)
can check `.available` before assuming a real browser can be launched,
instead of the whole Agent crashing on import when Playwright isn't
installed for this platform (Android/Termux).

`backend.browser.engine.BrowserEngine` (Playwright, desktop) implements
this today; `backend.browser.android_backend.AndroidBrowserBackend` is a
placeholder for a future Android/ADB-driven implementation. Nothing else
in the codebase is required to route through this base class -- existing
call sites keep constructing `BrowserEngine` directly and keep working
unchanged; this exists purely so the "is a real browser available here"
question has one clean, capability-checked answer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BrowserBackend(ABC):
    name: str = "browser_backend"

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this backend can actually drive a browser on the
        current platform right now (not just whether the class exists)."""

    @property
    def unavailable_reason(self) -> str | None:
        """Human-readable reason when `available` is False, or None when
        available/not yet checked. Surfaced by diagnostics so a missing
        browser backend shows up as a capability limitation, not a
        mysterious failure deep in a task."""
        return None
