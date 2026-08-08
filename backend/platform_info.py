"""
Platform capability detection.

Nexus-Agent runs on Windows/Linux/macOS desktops (full Playwright +
ChromaDB + psutil stack) as well as Android/Termux, where Playwright
cannot install natively, ChromaDB is often unavailable, and psutil may be
missing or only partially functional. Every module that touches one of
those optional dependencies should ask *this* module whether it's
available rather than doing its own `try: import X` -- so the platform
story is defined in exactly one place and stays consistent across
monitoring, memory, skills, and the browser engine.

Detecting "Android" by `platform.system()` alone doesn't work -- Termux
reports plain "Linux". We combine several Termux/Android-specific
environment signals instead (see `_detect_android_termux`).

Availability of an optional package is verified by actually importing it
(not just `importlib.util.find_spec`, which only proves the package is
*installed*, not that it *imports cleanly* -- relevant on Android where a
package can be partially installed and fail at import or first use).
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import sys
from dataclasses import dataclass
from functools import lru_cache


def _detect_android_termux() -> bool:
    """Best-effort Android/Termux detection using several independent
    signals, any one of which is sufficient. No single signal is fully
    reliable on its own across Termux versions/configurations."""
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" in prefix:
        return True
    if os.environ.get("TERMUX_VERSION"):
        return True
    if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_DATA"):
        return True
    if os.path.isdir("/data/data/com.termux"):
        return True
    try:
        if "android" in platform.uname().release.lower():
            return True
    except Exception:  # noqa: BLE001 - platform.uname() is best-effort
        pass
    try:
        if hasattr(sys, "getandroidapilevel"):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _probe_import(module_name: str) -> bool:
    """True only if `module_name` is both installed and importable."""
    try:
        if importlib.util.find_spec(module_name) is None:
            return False
    except (ImportError, ValueError):
        return False
    try:
        importlib.import_module(module_name)
        return True
    except Exception:  # noqa: BLE001 - any import-time failure means "unavailable"
        return False


@dataclass(frozen=True)
class PlatformCapabilities:
    system: str  # "Windows" | "Linux" | "Darwin" | "Android"
    is_windows: bool
    is_linux: bool
    is_macos: bool
    is_android: bool
    psutil_available: bool
    chromadb_available: bool
    playwright_available: bool

    @property
    def browser_playwright_available(self) -> bool:
        """Playwright is importable *and* this isn't Android -- Playwright
        has been observed to partially import on some Termux builds
        without being able to actually launch a browser, so Android is
        always treated as unavailable regardless of import success."""
        return self.playwright_available and not self.is_android

    def to_dict(self) -> dict:
        return {
            "system": self.system,
            "is_windows": self.is_windows,
            "is_linux": self.is_linux,
            "is_macos": self.is_macos,
            "is_android": self.is_android,
            "psutil_available": self.psutil_available,
            "chromadb_available": self.chromadb_available,
            "playwright_available": self.playwright_available,
            "browser_playwright_available": self.browser_playwright_available,
        }


@lru_cache(maxsize=1)
def get_capabilities() -> PlatformCapabilities:
    system = platform.system()
    is_android = _detect_android_termux()
    return PlatformCapabilities(
        system="Android" if is_android else system,
        is_windows=(system == "Windows"),
        is_linux=(system == "Linux" and not is_android),
        is_macos=(system == "Darwin"),
        is_android=is_android,
        psutil_available=_probe_import("psutil"),
        chromadb_available=_probe_import("chromadb"),
        playwright_available=_probe_import("playwright"),
    )


def refresh_capabilities() -> PlatformCapabilities:
    """Clears the cached detection result and recomputes it. Tests use
    this to simulate a different platform/dependency set within the same
    process (patching env vars / sys.modules first)."""
    get_capabilities.cache_clear()
    return get_capabilities()


# Module-level convenience singleton -- most call sites just want
# `from backend.platform_info import capabilities` and to read an
# attribute off it. It's computed once at import time; call
# `refresh_capabilities()` (and re-fetch `get_capabilities()`) if the
# process's dependency set changes at runtime (essentially test-only).
capabilities = get_capabilities()
