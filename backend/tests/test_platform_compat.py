"""
Tests for the Termux/Android compatibility layer:
  - backend/platform_info.py          (platform + dependency detection)
  - backend/search/*                  (VectorIndex abstraction + fallback)
  - backend/memory/store.py           (SQLite recall fallback)
  - backend/skills/library.py         (SQLite semantic_search fallback,
                                        CRUD/import without ChromaDB)
  - backend/monitoring/resources.py   (psutil unavailable)
  - backend/monitoring/diagnostics.py (missing optional deps reported as
                                        capability limitations, not failures)

These simulate "dependency unavailable" by monkeypatching the relevant
flags/objects directly (swapping in NullVectorIndex, patching
`psutil`/`capabilities`) rather than actually uninstalling packages, so
the whole suite stays fast and runs the same on every platform.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.config.settings import settings
from backend.database.models import MemoryEntry, Skill, SkillVersion
from backend.database.session import get_session, init_db
from backend.memory.store import MemoryStore
from backend.monitoring.diagnostics import DiagnosticsService
from backend.monitoring.resources import ResourceMonitor
from backend.platform_info import PlatformCapabilities, _detect_android_termux
from backend.search.null_index import NullVectorIndex
from backend.skills.library import SkillService


# ---------------------------------------------------------------- #
# platform_info: Android/Termux detection
# ---------------------------------------------------------------- #
def test_detect_android_termux_via_prefix_env(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("ANDROID_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_DATA", raising=False)
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert _detect_android_termux() is True


def test_detect_android_termux_via_termux_version_env(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
    assert _detect_android_termux() is True


def test_detect_android_termux_false_on_plain_linux(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("ANDROID_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_DATA", raising=False)
    assert _detect_android_termux() is False


def test_browser_playwright_available_forces_false_on_android():
    caps = PlatformCapabilities(
        system="Android",
        is_windows=False,
        is_linux=False,
        is_macos=False,
        is_android=True,
        psutil_available=False,
        chromadb_available=False,
        playwright_available=True,  # even if the import somehow succeeded
    )
    assert caps.browser_playwright_available is False


def test_browser_playwright_available_true_on_desktop_with_playwright():
    caps = PlatformCapabilities(
        system="Linux",
        is_windows=False,
        is_linux=True,
        is_macos=False,
        is_android=False,
        psutil_available=True,
        chromadb_available=True,
        playwright_available=True,
    )
    assert caps.browser_playwright_available is True


# ---------------------------------------------------------------- #
# backend/search: NullVectorIndex never raises
# ---------------------------------------------------------------- #
def test_null_vector_index_is_safe_noop():
    index = NullVectorIndex()
    assert index.available is False
    index.upsert(ids=["a"], documents=["doc"], metadatas=[{"k": "v"}])  # must not raise
    index.delete(ids=["a"])  # must not raise
    result = index.query(query_texts=["anything"], n_results=5)
    assert result == {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}


# ---------------------------------------------------------------- #
# Memory: save/recall without ChromaDB
# ---------------------------------------------------------------- #
@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_and_skills_db():
    await init_db()
    async with get_session() as session:
        await session.execute(delete(MemoryEntry))
        await session.execute(delete(SkillVersion))
        await session.execute(delete(Skill))
    yield
    async with get_session() as session:
        await session.execute(delete(MemoryEntry))
        await session.execute(delete(SkillVersion))
        await session.execute(delete(Skill))


@pytest.fixture
def memory_store_without_chroma(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
    store = MemoryStore()
    store._collection = NullVectorIndex()
    return store


class _Outcome:
    def __init__(self, status: str, steps: list, summary: str = "done"):
        self.status = status
        self.steps = steps
        self.summary = summary


@pytest.mark.asyncio
async def test_memory_save_without_chroma(memory_store_without_chroma):
    await memory_store_without_chroma.save_workflow_outcome(
        website="example.com", goal="log in", outcome=_Outcome("succeeded", [])
    )
    async with get_session() as session:
        rows = (await session.execute(delete(MemoryEntry).returning(MemoryEntry.id))).fetchall()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_memory_recall_without_chroma_uses_sqlite_ranking(memory_store_without_chroma):
    await memory_store_without_chroma.save_workflow_outcome(
        website="example.com", goal="log into the dashboard", outcome=_Outcome("succeeded", [])
    )
    await memory_store_without_chroma.save_workflow_outcome(
        website="other.com", goal="buy groceries", outcome=_Outcome("succeeded", [])
    )

    results = await memory_store_without_chroma.recall_similar_workflows("example.com", "log into the dashboard")
    assert len(results) == 1
    assert results[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_memory_recall_without_chroma_returns_empty_when_no_match(memory_store_without_chroma):
    results = await memory_store_without_chroma.recall_similar_workflows("nowhere.com", "do nothing at all")
    assert results == []


@pytest.mark.asyncio
async def test_memory_recall_falls_back_when_chroma_query_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
    store = MemoryStore()

    class BrokenIndex(NullVectorIndex):
        available = True  # reports available, but query() is broken

        def query(self, query_texts, n_results=5):
            raise RuntimeError("chroma index corrupted")

    store._collection = BrokenIndex()
    await store.save_workflow_outcome(website="example.com", goal="reset password", outcome=_Outcome("succeeded", []))

    results = await store.recall_similar_workflows("example.com", "reset password")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_duplicate_detection_uses_exact_hash_without_chroma(memory_store_without_chroma):
    outcome = _Outcome("succeeded", [])
    # Same website+goal -> same summary -> same content_hash -> folds into
    # one row via the write-time dedup guard (unrelated to chroma).
    await memory_store_without_chroma.save_workflow_outcome("example.com", "log in", outcome)
    await memory_store_without_chroma.save_workflow_outcome("example.com", "log in", outcome)

    async with get_session() as session:
        rows = (await session.execute(delete(MemoryEntry).returning(MemoryEntry.id))).fetchall()
    assert len(rows) == 1  # folded, not duplicated

    groups = await memory_store_without_chroma.find_duplicate_groups()
    assert groups == []  # nothing left to report as a "found later" duplicate


# ---------------------------------------------------------------- #
# Skills: CRUD / delete / semantic_search without ChromaDB
# ---------------------------------------------------------------- #
@pytest.fixture
def skill_service_without_chroma():
    svc = SkillService()
    svc._collection = NullVectorIndex()
    return svc


@pytest.mark.asyncio
async def test_skill_creation_without_chroma(skill_service_without_chroma):
    skill = await skill_service_without_chroma.create(name="Check gas price", trigger="check the gas price")
    assert skill["id"]
    fetched = await skill_service_without_chroma.get(skill["id"])
    assert fetched["name"] == "Check gas price"


@pytest.mark.asyncio
async def test_skill_deletion_without_chroma(skill_service_without_chroma):
    skill = await skill_service_without_chroma.create(name="Temp skill")
    assert await skill_service_without_chroma.delete(skill["id"]) is True
    assert await skill_service_without_chroma.get(skill["id"]) is None


@pytest.mark.asyncio
async def test_semantic_search_without_chroma_uses_keyword_ranking(skill_service_without_chroma):
    target = await skill_service_without_chroma.create(
        name="Gas price checker",
        description="Checks the current gas price on Etherscan",
        trigger="check the gas price\nwhat's gwei right now",
        category="defi",
    )
    await skill_service_without_chroma.create(name="Unrelated skill", description="buys groceries online")

    results = await skill_service_without_chroma.semantic_search("check the gas price")
    assert results
    assert results[0]["skill_id"] == target["id"]


@pytest.mark.asyncio
async def test_semantic_search_without_chroma_empty_when_no_skills(skill_service_without_chroma):
    results = await skill_service_without_chroma.semantic_search("anything at all")
    assert results == []


@pytest.mark.asyncio
async def test_rebuild_index_without_chroma_returns_successfully(skill_service_without_chroma):
    await skill_service_without_chroma.create(name="A")
    await skill_service_without_chroma.create(name="B")
    count = await skill_service_without_chroma.rebuild_index()
    assert count == 2  # no-op upserts, but the call succeeds and reports the count


@pytest.mark.asyncio
async def test_github_skill_import_without_chroma(monkeypatch, skill_service_without_chroma):
    from backend.skills.providers.base import SourceContext, SourceFile

    class MockProvider:
        def provider_name(self):
            return "github"

        def can_handle(self, url):
            return "github.com" in url

        async def fetch(self, url):
            return SourceContext(
                url=url,
                owner="mockowner",
                repo="mockrepo",
                branch="main",
                commit_sha="1234567890abcdef",
                primary_language="python",
                files=[SourceFile(relative_path="main.py", language="python", content="print('hi')")],
            )

    class MockExtractor:
        async def extract(self, ctx):
            return [
                {
                    "name": f"[{ctx.owner}/{ctx.repo}] Run Main Script",
                    "description": "Runs the main script",
                    "category": "workflow",
                    "trigger": "run main script",
                    "workflow": [{"action": "execute", "target": "shell", "value": "python main.py"}],
                    "website_hint": ctx.url,
                }
            ]

    import backend.skills.extractor as ext_mod
    import backend.skills.providers.registry as reg_mod

    mock_reg = reg_mod.ProviderRegistry()
    mock_reg.register(MockProvider())
    monkeypatch.setattr(reg_mod, "get_registry", lambda: mock_reg)
    monkeypatch.setattr(ext_mod, "SkillExtractor", MockExtractor)

    result = await skill_service_without_chroma.import_from_url("https://github.com/mockowner/mockrepo")

    assert result["skills_created"] == 1
    assert result["repository"] == "mockowner/mockrepo"
    assert result["skills"][0]["name"] == "[mockowner/mockrepo] Run Main Script"


# ---------------------------------------------------------------- #
# Monitoring: psutil unavailable
# ---------------------------------------------------------------- #
def test_resource_monitor_degrades_without_psutil(monkeypatch):
    import backend.monitoring.resources as resources_mod

    monkeypatch.setattr(resources_mod, "psutil", None)
    monitor = ResourceMonitor(app_state=SimpleNamespace(queue=None))
    snapshot = monitor.snapshot()

    assert snapshot.psutil_available is False
    assert snapshot.cpu_percent is None
    assert snapshot.process_rss_mb is None
    assert snapshot.system_memory_percent is None
    assert snapshot.browser_memory_mb is None
    # Queue/task stats must keep working regardless of psutil.
    assert snapshot.queue_size == 0
    assert snapshot.active_tasks == 0


def test_resource_monitor_survives_psutil_process_construction_failure(monkeypatch):
    """A partially-installed psutil (plausible on Termux) can be importable
    but fail on first real use -- ResourceMonitor must not crash at
    construction time in that case."""
    import backend.monitoring.resources as resources_mod

    class _BrokenPsutil:
        @staticmethod
        def Process(pid):
            raise OSError("psutil not fully functional on this platform")

    monkeypatch.setattr(resources_mod, "psutil", _BrokenPsutil)
    monitor = ResourceMonitor(app_state=SimpleNamespace(queue=None))
    assert monitor._process is None
    snapshot = monitor.snapshot()
    assert snapshot.cpu_percent is None


# ---------------------------------------------------------------- #
# Diagnostics: missing optional deps report as capability limitations
# ---------------------------------------------------------------- #
def _fake_capabilities(**overrides):
    base = dict(
        system="Linux",
        is_windows=False,
        is_linux=True,
        is_macos=False,
        is_android=False,
        psutil_available=True,
        chromadb_available=True,
        playwright_available=True,
    )
    base.update(overrides)
    return PlatformCapabilities(**base)


def test_diagnostics_playwright_check_degrades_on_android(monkeypatch):
    import backend.monitoring.diagnostics as diag_mod

    monkeypatch.setattr(
        diag_mod,
        "capabilities",
        _fake_capabilities(system="Android", is_linux=False, is_android=True, playwright_available=False),
    )
    service = DiagnosticsService(app_state=SimpleNamespace())
    check = service._check_playwright()
    assert check.passed is True
    assert "Unavailable" in check.detail


def test_diagnostics_chromadb_check_degrades_on_android(monkeypatch):
    import backend.monitoring.diagnostics as diag_mod

    monkeypatch.setattr(
        diag_mod,
        "capabilities",
        _fake_capabilities(system="Android", is_linux=False, is_android=True, chromadb_available=False),
    )
    service = DiagnosticsService(app_state=SimpleNamespace())
    check = service._check_chromadb()
    assert check.passed is True
    assert "SQLite fallback" in check.detail


def test_diagnostics_psutil_check_degrades_on_android(monkeypatch):
    import backend.monitoring.diagnostics as diag_mod

    monkeypatch.setattr(
        diag_mod,
        "capabilities",
        _fake_capabilities(system="Android", is_linux=False, is_android=True, psutil_available=False),
    )
    service = DiagnosticsService(app_state=SimpleNamespace())
    check = service._check_psutil()
    assert check.passed is True
    assert "degraded" in check.detail


def test_diagnostics_chromadb_check_fails_on_desktop_when_missing(monkeypatch):
    """On a real desktop platform (not Android), a missing optional
    dependency is still worth flagging as a genuine problem, not silently
    treated as a capability limitation."""
    import backend.monitoring.diagnostics as diag_mod

    monkeypatch.setattr(diag_mod, "capabilities", _fake_capabilities(chromadb_available=False))
    service = DiagnosticsService(app_state=SimpleNamespace())
    check = service._check_chromadb()
    assert check.passed is False


@pytest.mark.asyncio
async def test_diagnostics_memory_check_notes_fallback_without_chromadb(monkeypatch):
    import backend.monitoring.diagnostics as diag_mod

    monkeypatch.setattr(diag_mod, "capabilities", _fake_capabilities(chromadb_available=False))
    service = DiagnosticsService(app_state=SimpleNamespace(memory=object()))
    check = service._check_memory()
    assert check.passed is True
    assert "fallback" in check.detail.lower()
