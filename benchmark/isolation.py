"""Isolation helpers for stateful benchmark runs.

Provides a context manager that creates a fully isolated environment per run:
- Temp SQLite DB with OhioT1DM data
- Temp Qdrant knowledge base
- Temp chat sessions file

All global singletons (cgm_store, kb_store, CHAT_SESSIONS_PATH) are patched
so production tools write into the isolated temp directory transparently.
"""

from __future__ import annotations

import importlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from agent.data.cgm_store import CGMStore
from agent.kb.store import Embedder, InMemoryVectorBackend, KnowledgeBaseStore
from config import KB_EMBEDDING_MODEL
from agent.data.ohio_t1dm import load_ohio_t1dm_xml

cgm_store_module = importlib.import_module("agent.data.cgm_store")
tools_module = importlib.import_module("agent.tools")
kb_store_module = importlib.import_module("agent.kb.store")
session_store_module = importlib.import_module("agent.session_store")


class BenchmarkIsolation:
    """Manages isolated state (SQLite + KB + sessions) for a single benchmark run."""

    def __init__(self, xml_path: str, embedder: Any | None = None, keep: bool = False):
        self.temp_dir = tempfile.mkdtemp(prefix="diabetes_benchmark_")
        self.db_path = str(Path(self.temp_dir) / "benchmark_data.sqlite3")
        self.sessions_path = str(Path(self.temp_dir) / "chat_sessions.json")
        self._keep = keep
        Path(self.sessions_path).write_text("{}", encoding="utf-8")

        # Build DB from OhioT1DM XML
        self.counts = load_ohio_t1dm_xml(xml_path=xml_path, db_path=self.db_path)

        # Create empty isolated KB using in-memory backend — no persistence needed
        # for benchmark runs, and avoids Qdrant local SQLite transaction issues.
        self.kb_store = KnowledgeBaseStore.__new__(KnowledgeBaseStore)
        self.kb_store._embedder = embedder if embedder is not None else Embedder(KB_EMBEDDING_MODEL)
        self.kb_store._backend = InMemoryVectorBackend()
        for collection in self.kb_store.COLLECTIONS.values():
            self.kb_store._backend.ensure_collection(collection)

        # Patch global singletons
        self._patch()

    def _patch(self) -> CGMStore:
        """Patch module-level singletons to point at isolated instances."""
        self._old_cgm_store = getattr(cgm_store_module, "cgm_store", None)
        self._old_tools_cgm_store = getattr(tools_module, "cgm_store", None)
        self._old_kb_store = getattr(kb_store_module, "kb_store", None)
        self._old_tools_kb_store = getattr(tools_module, "kb_store", None)
        self._old_sessions_path = getattr(
            session_store_module, "CHAT_SESSIONS_PATH", None
        )

        new_cgm_store = CGMStore(db_path=self.db_path)
        setattr(cgm_store_module, "cgm_store", new_cgm_store)
        setattr(tools_module, "cgm_store", new_cgm_store)
        setattr(kb_store_module, "kb_store", self.kb_store)
        setattr(tools_module, "kb_store", self.kb_store)
        setattr(session_store_module, "CHAT_SESSIONS_PATH", self.sessions_path)

        return new_cgm_store

    def restore(self) -> None:
        """Restore all patched globals to their original values."""
        setattr(cgm_store_module, "cgm_store", self._old_cgm_store)
        setattr(tools_module, "cgm_store", self._old_tools_cgm_store)
        setattr(kb_store_module, "kb_store", self._old_kb_store)
        setattr(tools_module, "kb_store", self._old_tools_kb_store)
        if self._old_sessions_path is not None:
            setattr(session_store_module, "CHAT_SESSIONS_PATH", self._old_sessions_path)

    def cleanup(self) -> None:
        """Restore globals, close isolated stores, and delete temp directory."""
        self.restore()
        try:
            self.kb_store.close()
        except Exception:
            pass
        if not self._keep:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def __enter__(self) -> "BenchmarkIsolation":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cleanup()
