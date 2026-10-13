"""Part G — a semantic response cache that is safe, fresh, and keeps FAQ answers up in an outage.

* ``SemanticCache``: lookup by embedding similarity *within a scope* (prompt
  version; personal questions never), TTL + a stale window, invalidation by
  source document, bounded size (LRU).
* ``CachingClient``: wraps your ResilientClient. A fresh hit answers without
  calling any provider; a miss goes to the providers and the answer is stored;
  when the providers give up, a *stale* answer beats no answer (fallback
  ladder, rung 3: "cached response").

Tests: tests/test_cache.py, then `make cache` (tape C1).

Lifetime of an entry (age = now - stored_at):

    0 ......... ttl_s ................ ttl_s + max_stale_s ......
    |   fresh   |  stale: only if no provider answers  |  expired: drop it

Only FAQ calls carry ``call.request``; every other call has ``request=None``
and must go straight to the inner client.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from gameday_lab.sim.contracts import (  # noqa: F401
    Action,
    Answer,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    Request,
    Serve,
)


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """PROVIDED. Cosine similarity: 1.0 = same direction (high = strict threshold)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Entry:
    embedding: tuple[float, ...]
    answer: Answer
    stored_at: float
    last_used: float


@dataclass(frozen=True)
class CacheHit:
    answer: Answer
    similarity: float
    age_s: float
    fresh: bool  # age_s <= ttl_s; a stale hit may only be served when no provider can answer


class SemanticCache:
    def __init__(self, similarity_threshold: float = 0.9, ttl_s: float = 60.0, max_stale_s: float = 600.0,
                 max_entries: int = 500) -> None:
        """PROVIDED: reads the `cache:` block of configs/gameday.yaml."""
        self.similarity_threshold = similarity_threshold
        self.ttl_s = ttl_s
        self.max_stale_s = max_stale_s
        self.max_entries = max_entries
        self.entries: list[Entry] = []

    def lookup(self, req: Request, now: float) -> CacheHit | None:
        if req.personal:
            return None

        # Drop expired entries (age > ttl_s + max_stale_s)
        max_age = self.ttl_s + self.max_stale_s
        self.entries = [e for e in self.entries if (now - e.stored_at) <= max_age]

        best_entry: Entry | None = None
        best_sim = -1.0
        for entry in self.entries:
            if entry.answer.prompt_version == req.prompt_version:
                sim = cosine(req.embedding, entry.embedding)
                if sim > best_sim:
                    best_sim = sim
                    best_entry = entry

        if best_entry is not None and best_sim >= self.similarity_threshold:
            best_entry.last_used = now
            age_s = now - best_entry.stored_at
            return CacheHit(
                answer=best_entry.answer,
                similarity=best_sim,
                age_s=age_s,
                fresh=(age_s <= self.ttl_s),
            )
        return None

    def store(self, req: Request, answer: Answer, now: float) -> None:
        if req.personal:
            return

        # Check if an entry in the same scope is already >= similarity_threshold
        for entry in self.entries:
            if entry.answer.prompt_version == req.prompt_version:
                if cosine(req.embedding, entry.embedding) >= self.similarity_threshold:
                    entry.embedding = req.embedding
                    entry.answer = answer
                    entry.stored_at = now
                    entry.last_used = now
                    return

        # Otherwise append new entry
        self.entries.append(Entry(req.embedding, answer, stored_at=now, last_used=now))
        if len(self.entries) > self.max_entries:
            # Evict least recently used entry (smallest last_used)
            lru_idx = min(range(len(self.entries)), key=lambda i: self.entries[i].last_used)
            self.entries.pop(lru_idx)

    def invalidate(self, source: str, now: float) -> int:
        before = len(self.entries)
        self.entries = [e for e in self.entries if source not in e.answer.sources]
        return before - len(self.entries)


class CachingClient(BaseClient):
    name = "cached"

    def __init__(self, inner: BaseClient, cache: SemanticCache) -> None:
        """PROVIDED. ``inner`` is your ResilientClient (see factory.build_client("cached"))."""
        self.inner = inner
        self.cache = cache

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """PROVIDED."""
        self.inner.on_cancel(call, tag, now)

    # ------------------------------------------------------------------ TODO
    def start(self, call: CallContext, now: float) -> Action:
        if call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None and hit.fresh:
                return Serve(hit.answer, stale=False)
        action = self.inner.start(call, now)
        return self._or_stale(call, action, now)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        self.inner.on_success(attempt, now, latency_s)
        call = attempt.call
        if call.request is not None and call.answer is not None:
            self.cache.store(call.request, call.answer, now)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        action = self.inner.on_error(attempt, err, now)
        return self._or_stale(attempt.call, action, now)

    def on_content_update(self, source: str, now: float) -> None:
        self.cache.invalidate(source, now)
        self.inner.on_content_update(source, now)

    def _or_stale(self, call: CallContext, action: Action, now: float) -> Action:
        if isinstance(action, GiveUp) and call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None:
                return Serve(hit.answer, stale=not hit.fresh)
        return action
