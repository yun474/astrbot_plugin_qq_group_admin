from __future__ import annotations

from collections import OrderedDict
from time import monotonic


class ReviewCallbackGuard:
    """Bound callback work without caching successful authorization."""

    USER_COOLDOWN = 2
    DENIED_TTL = 120
    LOOKUP_CONCURRENCY = 2
    CACHE_LIMIT = 2048
    ERROR_LOG_INTERVAL = 60

    def __init__(self) -> None:
        self._cooldowns: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self._denied: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self._active_lookups = 0
        self._next_error_log: dict[str, float] = {}

    @staticmethod
    def _prune(cache: OrderedDict[tuple[str, ...], float], now: float) -> None:
        # Each cache has one fixed TTL, so insertion order is expiry order.
        while cache and next(iter(cache.values())) <= now:
            cache.popitem(last=False)

    def _remember(
        self, cache: OrderedDict[tuple[str, ...], float], key: tuple[str, ...], ttl: int
    ) -> None:
        now = monotonic()
        self._prune(cache, now)
        cache[key] = now + ttl
        cache.move_to_end(key)
        if len(cache) > self.CACHE_LIMIT:
            cache.popitem(last=False)

    def check_click(
        self, platform: str, group: str, sender: str, *, assigned_admin: bool
    ) -> int | None:
        now = monotonic()
        self._prune(self._denied, now)
        self._prune(self._cooldowns, now)
        key = (platform, group, sender)
        if assigned_admin:
            # Granting plugin/AstrBot admin rights takes effect immediately.
            self._denied.pop(key, None)
        elif key in self._denied:
            return 4
        if (platform, sender) in self._cooldowns:
            return 2
        self._remember(self._cooldowns, (platform, sender), self.USER_COOLDOWN)
        return None

    def remember_denied(self, platform: str, group: str, sender: str) -> None:
        self._remember(self._denied, (platform, group, sender), self.DENIED_TTL)

    def start_lookup(self) -> bool:
        # Reject excess work immediately; do not queue requests behind a semaphore.
        if self._active_lookups >= self.LOOKUP_CONCURRENCY:
            return False
        self._active_lookups += 1
        return True

    def finish_lookup(self) -> None:
        self._active_lookups -= 1

    def should_log_error(self, kind: str) -> bool:
        now = monotonic()
        if now < self._next_error_log.get(kind, 0):
            return False
        self._next_error_log[kind] = now + self.ERROR_LOG_INTERVAL
        return True
