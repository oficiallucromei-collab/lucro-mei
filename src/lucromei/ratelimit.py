"""In-process rate limiting for the public webhook.

Meta signs every webhook, so a forged request is already rejected. The limits below keep a
flood of unsigned or oversized requests from using the instance's CPU and database: each
client gets a budget of requests per minute, and a much smaller one for requests that failed
the signature check. Counters live in memory, per instance (Cloud Run may run several); they
are a guard against abuse, not an exact quota.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class SlidingWindowLimiter:
    limit: int
    window_seconds: float = 60.0
    max_keys: int = 10_000
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str, now: float | None = None) -> bool:
        """Count one hit for `key`; False when the key already used its budget."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self.max_keys:
                    self._evict(cutoff)
                hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def blocked(self, key: str, now: float | None = None) -> bool:
        """Has `key` used its budget, without counting a new hit?"""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return False
            return sum(1 for hit in hits if hit > now - self.window_seconds) >= self.limit

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _evict(self, cutoff: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        if len(self._hits) >= self.max_keys:  # still full: drop the oldest half
            for key in list(self._hits)[: self.max_keys // 2]:
                del self._hits[key]


def client_key(forwarded_for: str | None, peer: str | None) -> str:
    """The caller's address. On Cloud Run the last X-Forwarded-For hop is added by Google's
    front end and cannot be forged by the client; earlier hops can."""
    if forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            return hops[-1][:64]
    return (peer or "unknown")[:64]
