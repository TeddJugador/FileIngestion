"""Removing a version's footprint: its vectors (via its key list), the list itself and
the source copy. Shared by the indexer (superseded versions) and retire."""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.registry import Registry
from knowledge.sources import SourceStore
from knowledge.vector_store import VectorStore


@dataclass
class VersionCleaner:
    registry: Registry
    sources: SourceStore
    store: VectorStore
    index_name: str

    def discard(self, school_id: str, document_id: str, version: str) -> None:
        record = self.registry.get_version(school_id, document_id, version)
        if record is None:
            return
        if record.vector_keys_key:
            keys = self.sources.get_vector_keys(record.vector_keys_key)
            if record.index_name in ("", self.index_name):
                self.store.delete(keys)  # vectors in an older index go with that index
            self.sources.delete(record.vector_keys_key)
        self.sources.delete(record.source_key)
