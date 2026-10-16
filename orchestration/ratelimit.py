"""RatePolicy / RetryPolicy -- forge-pipeline-v1.md §9.

No core-level defaults, ever -- every connector supplies its own values from its
own manifest/documented limits. Forge stays generic as APIs and their limits change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RetryPolicy:
    retryable: frozenset[Literal["transient", "rate_limit"]]  # NOT "auth", "malformed_payload", "schema_mismatch"
    max_attempts: int
    request_hash_cache: bool = True


@dataclass(frozen=True)
class RatePolicy:
    requests_per_second: float | None = None
    max_concurrency: int | None = None
    batch_size: int | None = None
    retry_policy: RetryPolicy | None = None


# Real, documented limits per source (connector-research.md §2) -- these live here,
# not as defaults baked into the RatePolicy type itself.
ENSEMBL_RATE_POLICY = RatePolicy(
    requests_per_second=15, max_concurrency=8, batch_size=1000,
    retry_policy=RetryPolicy(frozenset({"transient", "rate_limit"}), max_attempts=3),
)
CLINVAR_RATE_POLICY = RatePolicy(
    # NCBI E-utilities: 3 req/s without an API key, 10 req/s with one (connector-research.md §12)
    requests_per_second=3, max_concurrency=1, batch_size=200,
    retry_policy=RetryPolicy(frozenset({"transient", "rate_limit"}), max_attempts=5),
)
GNOMAD_RATE_POLICY = RatePolicy(
    requests_per_second=5, max_concurrency=4, batch_size=1,  # batched via GraphQL query shape, not HTTP batching
    retry_policy=RetryPolicy(frozenset({"transient", "rate_limit"}), max_attempts=3),
)
CLINGEN_RATE_POLICY = RatePolicy(
    requests_per_second=2, max_concurrency=1, batch_size=1,
    retry_policy=RetryPolicy(frozenset({"transient", "rate_limit"}), max_attempts=3),
)
