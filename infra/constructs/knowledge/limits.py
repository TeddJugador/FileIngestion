"""Sizes and timings shared by the knowledge constructs (and their tests)."""

from aws_cdk import Duration

INDEXER_TIMEOUT = Duration.seconds(600)
INDEXER_MEMORY_MB = 1536
INDEXER_MAX_CONCURRENCY = 2  # keeps embedding calls inside provider rate limits
INDEXING_MAX_RECEIVE_COUNT = 3
DLQ_RETENTION = Duration.days(14)

# Vector metadata. Filterable keys are what queries filter on (2 KB per vector in total);
# everything else is declared non-filterable at index creation (at most 10 keys; 40 KB of
# metadata per vector including filterable).
FILTERABLE_METADATA_KEYS = ("school_id", "document_id", "version", "audience")
NON_FILTERABLE_METADATA_KEYS = ("chunk_text", "title", "page_start", "page_end", "heading_path")
