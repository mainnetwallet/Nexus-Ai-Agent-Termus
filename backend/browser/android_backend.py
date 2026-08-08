"""
AndroidBrowserBackend -- placeholder for a future Android/ADB-driven
browser backend.

Playwright cannot install natively under Termux (no matching Chromium/
driver build), so browser automation is simply unavailable on Android
today. This class exists so that story has a real, capability-checked
home in the architecture (`available = False`, a clear
`unavailable_reason`) instead of the Agent either crashing on import or
silently pretending browser automation works. When a real Android
backend is implemented (e.g. driving a device browser over ADB/
accessibility APIs), it replaces this class's body without any caller
needing to change how it checks for browser availability.
"""
from __future__ import annotations

from backend.browser.backend_base import BrowserBackend


class AndroidBrowserBackend(BrowserBackend):
    name = "android"

    @property
    def available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str | None:
        return (
            "Browser automation is not yet implemented for Android/Termux "
            "(Playwright cannot install natively on this platform). "
            "Task automation, wallet auto-approval, and browser-dependent "
            "connectors are unavailable until a device-driven backend is added."
        )
