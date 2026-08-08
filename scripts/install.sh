#!/usr/bin/env bash
#
# Nexus-Agent -- platform-aware dependency install.
#
# Usage (from the repo root):
#   chmod +x scripts/install.sh   # first time only
#   ./scripts/install.sh
#
# On Windows/Linux/macOS this installs requirements.txt (the full desktop
# set: chromadb + playwright + psutil included) and, when playwright is
# present, its browser binaries.
#
# On Android/Termux -- detected the same way backend/platform_info.py
# detects it (Termux sets $PREFIX to something containing "com.termux") --
# this installs requirements-core.txt instead, skipping the three packages
# that cannot install natively there. The Agent still runs: ChromaDB falls
# back to SQLite keyword ranking, psutil-based resource metrics report as
# unavailable instead of crashing, and browser automation reports itself
# unavailable until a future Android-native backend lands (see
# backend/browser/android_backend.py). See DiagnosticsService
# (backend/monitoring/diagnostics.py) for how each shows up at runtime.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

is_termux() {
    [[ "${PREFIX:-}" == *com.termux* ]] || [[ -n "${TERMUX_VERSION:-}" ]] || [[ -d "/data/data/com.termux" ]]
}

if is_termux; then
    echo "Termux/Android detected -- installing requirements-core.txt"
    echo "(chromadb, playwright, and psutil are skipped; the Agent uses"
    echo " built-in fallbacks for all three -- see README's Android section)"
    pip install -r requirements-core.txt
else
    echo "Installing requirements.txt (full desktop dependency set)"
    pip install -r requirements.txt
    if python -c "import playwright" >/dev/null 2>&1; then
        echo "Installing Playwright browser binaries..."
        python -m playwright install chromium
    fi
fi

echo "Done."
