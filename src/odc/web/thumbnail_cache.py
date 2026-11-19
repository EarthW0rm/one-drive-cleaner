# SPDX-License-Identifier: GPL-3.0-only
"""Cache em memória de URLs de miniatura pré-autenticadas (TTL curto, LRU limitado).

As URLs expiram na CDN, por isso o TTL (30 min) é conservador. O cache nunca vai a disco nem a
log/telemetria: a URL é credencial de acesso temporária (RN-02, Zero Trust). A chave é o
`drive_item_id` (não o id interno), para que a troca de conta não reaproveite entradas alheias.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from typing import Final

DEFAULT_TTL_SECONDS: Final = 30 * 60.0
DEFAULT_MAX_ENTRIES: Final = 5_000


class ThumbnailCache:
    """Mapa `drive_item_id -> URL` com expiração por tempo e despejo LRU."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            message = "ttl_seconds e max_entries devem ser positivos"
            raise ValueError(message)
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get_many(self, keys: Iterable[str]) -> dict[str, str]:
        """URLs vigentes das chaves (as expiradas são descartadas; acerto renova o LRU)."""
        now = self._clock()
        found: dict[str, str] = {}
        with self._lock:
            for key in keys:
                entry = self._entries.get(key)
                if entry is None:
                    continue
                expires_at, url = entry
                if expires_at <= now:
                    del self._entries[key]
                    continue
                self._entries.move_to_end(key)
                found[key] = url
        return found

    def put_many(self, urls: Mapping[str, str]) -> None:
        expires_at = self._clock() + self._ttl
        with self._lock:
            for key, url in urls.items():
                self._entries[key] = (expires_at, url)
                self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def discard_many(self, keys: Iterable[str]) -> None:
        with self._lock:
            for key in keys:
                self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
